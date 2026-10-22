"""合成账号和原文验证真实路由、权限、迁移与上传处理；不使用正式库或供应商。"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from app import migrations
from app.config import Settings
from app.main import create_app
from app.parse_worker import ParseWorker
from app.product_access import COOKIE, ProductAccess, verify_password
from app.question_worker import QuestionWorker
from app.repository import Repository
from tests.test_question_jobs import Embedding, LocalRuntime, Model, QUESTION, TEXT, question_fixture

PASSWORD = 'Synthetic-only-password-2026'


@pytest.fixture
def team(tmp_path, monkeypatch):
    env = question_fixture(tmp_path, team_mode=True, session_cookie_secure=False)
    access = ProductAccess(env.repository, env.settings, env.rag.index)
    users = {name: access.create_user(name, name, PASSWORD, role) for name, role in
             [('admin', 'admin'), ('alice', 'customer'), ('bob', 'customer')]}
    monkeypatch.setattr('app.main.APIEmbedding', Embedding)
    monkeypatch.setattr('app.main.DeepSeekModel', lambda settings: Model())
    app = create_app(env.settings)
    with ExitStack() as stack:
        clients = {name: stack.enter_context(TestClient(app)) for name in [*users, 'anonymous']}
        for name in users:
            response = clients[name].post('/api/auth/login', json={'username': name, 'password': PASSWORD})
            assert response.status_code == 200
            clients[name].headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield env, users, clients


def publish(clients, users):
    admin = clients['admin']
    base = admin.post('/api/knowledge-bases', json={'name': '合成知识库'}).json()
    assert admin.post(f'/api/knowledge-bases/{base["id"]}/documents', json={'document_id': 'qdoc'}).status_code == 200
    for name in ['alice', 'bob']:
        assert admin.put(f'/api/knowledge-bases/{base["id"]}/members/{users[name]["id"]}', json={'enabled': True}).status_code == 200
    response = admin.post(f'/api/knowledge-bases/{base["id"]}/publish', json={'expected_revision': 0})
    assert response.status_code == 200, response.text
    return response.json()


def test_anonymous_and_csrf_cannot_use_old_or_new_entrypoints(team):
    _, _, clients = team
    anonymous, admin = clients['anonymous'], clients['admin']
    for path in ['/api/documents', '/api/documents/qdoc/original', '/api/runtime/status', '/docs', '/openapi.json']:
        assert anonymous.get(path).status_code == 401
    response = anonymous.get('/app', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/login'
    assert anonymous.get('/static/workspace.js').status_code == 200
    assert admin.post('/api/knowledge-bases', json={'name': '伪请求'}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert admin.post('/api/knowledge-bases', json={'name': '伪请求'}, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    assert admin.post('/api/auth/logout', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_drafts_and_unassigned_legacy_documents_are_private(team):
    env, users, clients = team
    base = clients['admin'].post('/api/knowledge-bases', json={'name': '草稿库'}).json()
    clients['admin'].post(f'/api/knowledge-bases/{base["id"]}/documents', json={'document_id': 'qdoc'})
    clients['admin'].put(f'/api/knowledge-bases/{base["id"]}/members/{users["alice"]["id"]}', json={'enabled': True})
    assert clients['alice'].get('/api/documents').json() == []
    assert clients['alice'].get('/api/knowledge-bases').json() == []
    assert clients['alice'].get('/api/documents/qdoc').status_code == 404
    assert not env.embedding.calls and not env.model.calls


@pytest.mark.parametrize('method,path', [
    ('get', '/admin'), ('get', '/tools'), ('get', '/knowledge'), ('get', '/docs'),
    ('get', '/api/model/status'), ('post', '/api/model/test'), ('get', '/api/runtime/status'),
    ('post', '/api/documents'), ('post', '/api/documents/qdoc/parse'),
    ('post', '/api/documents/qdoc/index'), ('post', '/api/documents/qdoc/questions'),
    ('get', '/api/documents/qdoc/analysis-jobs'), ('get', '/api/admin/users'),
    ('get', '/api/parse-tasks/guessed'), ('get', '/api/analysis-results/guessed/export'),
])
def test_customers_cannot_enter_legacy_admin_operations(team, method, path):
    _, users, clients = team
    publish(clients, users)
    assert getattr(clients['alice'], method)(path).status_code == 403


def test_published_access_private_question_ownership_and_revocation(team):
    env, users, clients = team
    base = publish(clients, users)
    alice, bob, admin = clients['alice'], clients['bob'], clients['admin']
    assert len(alice.get('/api/documents').json()) == 1
    assert alice.get('/api/documents/qdoc/chunks').json()[0]['text'] == TEXT
    jobs = []
    for client in [alice, bob]:
        response = client.post('/api/documents/qdoc/question-jobs', json={'question': QUESTION, 'idempotency_key': 'same-browser-key'})
        assert response.status_code == 202, response.text
        job = response.json()['job']; jobs.append(job)
        assert job['idempotency_key'] == 'same-browser-key'
    assert jobs[0]['id'] != jobs[1]['id']
    assert alice.post('/api/documents/qdoc/question-jobs', json={'question': QUESTION, 'idempotency_key': 'same-browser-key'}).json()['job']['id'] == jobs[0]['id']
    assert alice.get(f'/api/question-jobs/{jobs[1]["id"]}').status_code == 404
    assert admin.get(f'/api/question-jobs/{jobs[0]["id"]}').status_code == 404
    assert len(alice.get('/api/documents/qdoc/question-jobs').json()) == 1
    worker = QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime())
    assert worker.run_job(jobs[0]['id'])
    finished = alice.get(f'/api/question-jobs/{jobs[0]["id"]}').json()
    assert finished['status'] == 'succeeded'
    result_id = finished['result_id']
    assert alice.get(f'/api/question-results/{result_id}/export').status_code == 200
    assert bob.get(f'/api/question-results/{result_id}').status_code == 404
    assert admin.get(f'/api/question-results/{result_id}/export').status_code == 404
    admin.put(f'/api/knowledge-bases/{base["id"]}/members/{users["alice"]["id"]}', json={'enabled': False})
    assert alice.get('/api/documents').json() == []
    for path in ['/api/documents/qdoc/chunks', '/api/documents/qdoc/original', '/api/documents/qdoc/question-jobs',
                 f'/api/question-results/{result_id}', f'/api/question-results/{result_id}/export', f'/api/question-jobs/{jobs[0]["id"]}']:
        assert alice.get(path).status_code == 404


def test_disabling_user_invalidates_existing_cookie_and_last_admin_is_kept(team):
    env, users, clients = team
    admin, alice = clients['admin'], clients['alice']
    assert admin.patch(f'/api/admin/users/{users["admin"]["id"]}', json={'active': False}).status_code == 409
    assert admin.patch(f'/api/admin/users/{users["alice"]["id"]}', json={'active': False}).status_code == 200
    assert alice.get('/api/documents').status_code == 401
    with env.repository.connect() as db:
        row = db.execute('SELECT password_hash FROM product_users WHERE id=?', (users['admin']['id'],)).fetchone()
    assert PASSWORD not in row[0] and verify_password(PASSWORD, row[0])
    assert not verify_password('wrong-password', row[0])


def test_login_rate_limit_and_logout_persist_across_app_instances(team):
    env, _, clients = team
    anonymous, admin = clients['anonymous'], clients['admin']
    for _ in range(5):
        assert anonymous.post('/api/auth/login', json={'username': 'nobody', 'password': 'wrong'}).status_code == 401
    assert anonymous.post('/api/auth/login', json={'username': 'nobody', 'password': 'wrong'}).status_code == 429
    cookie = admin.cookies.get(COOKIE)
    assert admin.post('/api/auth/logout').status_code == 200
    admin.cookies.set(COOKIE, cookie)
    assert admin.get('/api/documents').status_code == 401
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM product_login_limits').fetchone()[0] > 0


def test_failed_publish_keeps_snapshot_and_stale_revision_is_rejected(team):
    env, users, clients = team
    base = publish(clients, users); admin = clients['admin']
    assert admin.post(f'/api/knowledge-bases/{base["id"]}/publish', json={'expected_revision': 0}).status_code == 409
    response = admin.post('/api/documents', files={'file': ('合成新资料.txt', '尚未解析的合成资料。'.encode())})
    identifier = response.json()['document']['id']
    admin.post(f'/api/knowledge-bases/{base["id"]}/documents', json={'document_id': identifier})
    assert admin.post(f'/api/knowledge-bases/{base["id"]}/publish', json={'expected_revision': 1}).status_code == 409
    current = admin.get(f'/api/knowledge-bases/{base["id"]}').json()
    assert current['active_snapshot_id'] == base['active_snapshot_id']
    assert len(clients['alice'].get('/api/documents').json()) == 1
    assert not env.embedding.calls and not env.model.calls


def test_revoked_queued_job_makes_no_provider_request(team):
    env, users, clients = team
    base = publish(clients, users)
    response = clients['alice'].post('/api/documents/qdoc/question-jobs', json={'question': QUESTION, 'idempotency_key': 'revoke-test'})
    assert response.status_code == 202, response.text
    job = response.json()['job']
    clients['admin'].put(f'/api/knowledge-bases/{base["id"]}/members/{users["alice"]["id"]}', json={'enabled': False})
    worker = QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime())
    worker.run_job(job['id'])
    assert not env.embedding.calls and not env.model.calls
    assert env.service.store.row(job['id'])['result_id'] is None
    assert env.service.store.row(job['id'])['status'] == 'cancelled'


def test_unpublished_new_parse_and_index_do_not_replace_customer_snapshot(team):
    from app.schemas import Chunk, SourceLocation
    env, users, clients = team
    base = publish(clients, users)
    old_index = base['published_documents'][0]['index_id']
    # 管理员准备一份不同的草稿；客户仍可对旧发布版提问。
    env.repository.finish_parse('qdoc', 1, [Chunk(id='draft-c', document_id='qdoc', page=1,
        text='这是尚未公开的新稿，包含99个部分。', parse_version_id='draft-v', order_index=0, chunk_type='text',
        sources=[SourceLocation(format='txt', line_start=1, line_end=1)])])
    env.rag.index.build('qdoc', rebuild=True)
    env.embedding.calls.clear()
    client = clients['alice']
    document = client.get('/api/documents/qdoc').json()
    assert document['active_parse_version_id'] == 'qv1'
    assert client.get('/api/documents/qdoc/index').json()['id'] == old_index
    assert '99' not in client.get('/api/documents/qdoc/chunks').text
    assert client.get('/api/documents/qdoc/chunks?version_id=draft-v').status_code == 404
    assert [v['id'] for v in client.get('/api/documents/qdoc/versions').json()] == ['qv1']
    response = client.post('/api/documents/qdoc/question-jobs', json={'question': QUESTION, 'idempotency_key': 'published-only'})
    assert response.status_code == 202, response.text
    job = response.json()['job']
    assert job['parse_version_id'] == 'qv1' and job['index_id'] == old_index
    QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job['id'])
    finished = client.get(f'/api/question-jobs/{job["id"]}').json()
    assert finished['status'] == 'succeeded'
    result = client.get(f'/api/question-results/{finished["result_id"]}').json()
    assert '21' in result['answer']['answer'] and '99' not in result['answer']['answer']


def test_auth_validation_never_echoes_password_and_secure_cookie_defaults(tmp_path):
    repository = Repository(tmp_path / 'docqa.db'); repository.initialize()
    settings = Settings(data_dir=tmp_path, team_mode=True)
    ProductAccess(repository, settings, None).create_user('admin', '合成管理员', PASSWORD, 'admin')
    with TestClient(create_app(settings), base_url='https://testserver') as client:
        bad = client.post('/api/auth/login', json={'username': 'a', 'password': PASSWORD})
        assert bad.status_code == 422 and PASSWORD not in bad.text
        response = client.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD})
        cookie = response.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'secure' in cookie and 'samesite=strict' in cookie
        assert response.headers['cache-control'] == 'no-store'


def test_upload_idempotency_survives_concurrent_posts_and_local_txt_parse(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        def upload(content=TEXT):
            return client.post('/api/documents', files={'file': ('合成上传资料.txt', content.encode())}, headers={'Idempotency-Key': 'upload-one'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: upload(), range(2)))
        assert all(r.status_code == 201 for r in responses)
        identifier = responses[0].json()['document']['id']
        assert responses[1].json()['document']['id'] == identifier
        assert len(client.get('/api/documents').json()) == 1
        assert len(list((tmp_path / 'uploads').iterdir())) == 1
        assert upload('不同资料').status_code == 409
        assert len(list((tmp_path / 'uploads').iterdir())) == 1
        submitted = client.post(f'/api/documents/{identifier}/parse', json={'idempotency_key': 'parse-one'}).json()
        worker = ParseWorker(settings); worker.runtime.mark_ready()
        assert worker.run_task(submitted['task']['id'])
        assert client.get(f'/api/documents/{identifier}').json()['active_parse_version_id']
        assert TEXT in ''.join(c['text'] for c in client.get(f'/api/documents/{identifier}/chunks').json())


def test_v6_migration_is_atomic_and_old_documents_remain_private(tmp_path):
    path = tmp_path / 'docqa.db'
    with sqlite3.connect(path, isolation_level=None) as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        for version in range(1, 6):
            migrations.MIGRATIONS[version](db)
            db.execute('INSERT INTO schema_migrations VALUES (?,?)', (version, 'synthetic'))
        db.execute("INSERT INTO documents(id,filename,size,created_at,status) VALUES ('keep','合成旧资料.txt',1,'synthetic','uploaded')")
        before = tuple(db.execute('SELECT * FROM documents').fetchone())
        migrations.MigrationHook.register(6, lambda: (_ for _ in ()).throw(RuntimeError('synthetic migration failure')))
        try:
            with pytest.raises(migrations.MigrationError):
                migrations.run_migrations(db, backup_dir=tmp_path / 'backups', db_path=path)
        finally:
            migrations.MigrationHook.clear()
        assert migrations.detect_version(db) == 5
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='product_users'").fetchone() is None
        migrations.run_migrations(db)
        assert migrations.validate_schema(db) == migrations.SCHEMA_VERSION
        assert tuple(db.execute('SELECT * FROM documents').fetchone()) == before
        for table in ['product_users', 'knowledge_base_members', 'knowledge_base_snapshots']:
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        # v14只添加私有历史资料投影，不向团队发布或授予客户权限。
        assert tuple(db.execute('SELECT kind,active_snapshot_id FROM knowledge_bases').fetchone())==('legacy',None)
        assert migrations.run_migrations(db)['applied'] == []
