"""合成临时资料验收：真实HTTP/解析/SQLite，不调用收费供应商。"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import migrations
from app.attachment_maintenance import purge_expired
from app.parse_worker import ParseWorker
from app.question_worker import QuestionWorker
from app.repository import Repository, now_iso
from tests.test_product_access import team
from tests.test_question_jobs import TEXT, QUESTION, LocalRuntime


def upload(client, key='temporary-one', text=TEXT):
    return client.post('/api/conversations/temporary', headers={'Idempotency-Key': key},
                       files={'file': ('synthetic.txt', text.encode(), 'text/plain')})


def ready(team):
    env, _, clients = team
    r = upload(clients['alice']); assert r.status_code == 201, r.text
    c = r.json(); path = '/api/conversations/' + c['id']
    r = clients['alice'].post(path + '/attachment/parse', headers={'Idempotency-Key': 'parse-once'})
    assert r.status_code == 202, r.text
    task = r.json()['task']
    assert ParseWorker(env.settings, env.repository).run_task(task['id'])
    assert env.repository.get_task(task['id']).status == 'succeeded'
    r = clients['alice'].post(path + '/attachment/index'); assert r.status_code == 200, r.text
    return c, path


def expire(env, doc):
    with env.repository.connect() as db:
        db.execute("UPDATE conversation_attachments SET expires_at='2000-01-01T00:00:00.000000+00:00' WHERE document_id=?", (doc,))


def test_upload_atomic_idempotent_private_and_never_published(team):
    env, _, clients = team
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: upload(clients['alice']), range(2)))
    assert [r.status_code for r in responses] == [201, 201]
    c = responses[0].json(); assert c['id'] == responses[1].json()['id']
    doc = c['document_id']; path = '/api/conversations/' + c['id']
    assert not env.embedding.calls and not env.model.calls
    assert upload(clients['alice'], text='different').status_code == 409
    for name in ['alice', 'bob', 'admin']:
        assert doc not in [d['id'] for d in clients[name].get('/api/documents').json()]
    for name in ['bob', 'admin']:
        assert clients[name].get(path).status_code == 404
        for suffix in ['', '/original', '/chunks', '/content', '/index']:
            assert clients[name].get('/api/documents/' + doc + suffix).status_code == 404
    base = clients['admin'].post('/api/knowledge-bases', json={'name': '合成库'}).json()
    assert clients['admin'].post(f'/api/knowledge-bases/{base["id"]}/documents', json={'document_id': doc}).status_code == 409
    assert clients['alice'].post('/api/conversations', json={'document_id': doc, 'idempotency_key': 'other'}).status_code == 409
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_attachments').fetchone()[0] == 1


def test_prepare_followup_sources_restart_read_and_no_implicit_calls(team):
    env, _, clients = team; c, path = ready(team); alice = clients['alice']
    assert len(env.embedding.calls) == 0  # Web索引桩与独立worker桩实例不同。
    detail = alice.get(path).json(); task = detail['document']['latest_task_id']
    assert alice.get('/api/parse-tasks/' + task).status_code == 200
    for name in ['admin', 'bob']:
        assert clients[name].get('/api/parse-tasks/' + task).status_code == 404
    for n in range(2):
        r = alice.post(path + '/turns', json={'question': QUESTION, 'idempotency_key': 'turn-' + str(n)})
        assert r.status_code == 202, r.text
        job = r.json()['job']; assert QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job['id'])
        assert env.service.get_job(job['id'])['status'] == 'succeeded'
    calls = (len(env.embedding.calls), len(env.model.calls))
    detail = alice.get(path).json(); assert len(detail['turns']) == 2
    result = detail['turns'][0]['result']; assert alice.get('/api/question-results/' + result['id'] + '/export').status_code == 200
    assert alice.get('/api/documents/' + c['document_id'] + '/original').status_code == 200
    for _ in range(3):
        assert alice.get(path).status_code == 200
        assert alice.get('/api/documents/' + c['document_id'] + '/versions').status_code == 200
    assert calls == (len(env.embedding.calls), len(env.model.calls))
    assert alice.post('/api/documents/' + c['document_id'] + '/question-jobs', json={'question': QUESTION}).status_code == 403


def test_expiry_blocks_read_export_retry_and_queued_execution(team):
    env, _, clients = team; c, path = ready(team); alice = clients['alice']
    r = alice.post(path + '/turns', json={'question': QUESTION, 'idempotency_key': 'one'}); job = r.json()['job']
    expire(env, c['document_id'])
    before = (len(env.embedding.calls), len(env.model.calls))
    assert QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job['id'])
    assert before == (len(env.embedding.calls), len(env.model.calls))
    for url in [path, '/api/documents/' + c['document_id'] + '/original', '/api/question-jobs/' + job['id']]:
        assert alice.get(url).status_code == 410
    assert alice.post(path + '/turns', json={'question': QUESTION, 'idempotency_key': 'two'}).status_code == 410
    assert upload(alice).status_code == 410


def test_remove_stops_worker_publication_and_purge_preserves_ledger(team):
    env, _, clients = team; c, path = ready(team); alice = clients['alice']
    r = alice.post(path + '/turns', json={'question': QUESTION, 'idempotency_key': 'one'}); job = r.json()['job']
    worker = QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime())
    assert worker.run_job(job['id'])
    result = env.service.get_job(job['id'])['result_id']
    assert result
    with env.repository.connect() as db:
        ledger = [tuple(r) for r in db.execute('SELECT * FROM question_calls ORDER BY id')]
    assert alice.delete(path + '/attachment').status_code == 200
    assert alice.get('/api/question-results/' + result + '/export').status_code == 410
    data = env.settings.data_dir
    # 真实运行守卫拒绝在线清理；测试用ready模拟进程存活。
    import os
    (data / '.local-service' / 'web.ready').write_text(json.dumps({'pid': os.getpid()}))
    with pytest.raises(RuntimeError, match='先停止'):
        purge_expired(data)
    for p in (data / '.local-service').glob('*.ready'):
        p.unlink()
    assert purge_expired(data)['purged'] == [c['document_id']]
    assert purge_expired(data)['purged'] == []
    assert not (data / 'uploads' / c['document_id']).exists()
    with env.repository.connect() as db:
        assert ledger == [tuple(r) for r in db.execute('SELECT * FROM question_calls ORDER BY id')]
        assert db.execute('SELECT COUNT(*) FROM chunks WHERE document_id=?', (c['document_id'],)).fetchone()[0] == 0
        assert db.execute('SELECT snapshot_json FROM question_jobs WHERE id=?', (job['id'],)).fetchone()[0] == '{}'
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None
    assert env.repository.chunks('qdoc')  # 普通旧文档不被清理。


def test_limits_csrf_rollback_no_orphan_files(team):
    env, _, clients = team; alice = clients['alice']
    assert upload(clients['anonymous']).status_code == 401
    assert alice.post('/api/conversations/temporary', headers={'X-CSRF-Token': ''}, files={'file': ('t.txt', b'x')}).status_code == 403
    for n in range(env.settings.attachment_max_active):
        assert upload(alice, key=str(n)).status_code == 201
    assert upload(alice, key='over').status_code == 409
    with env.repository.connect() as db:
        docs = {r[0] for r in db.execute('SELECT document_id FROM conversation_attachments')}
    assert {p.name for p in (env.settings.data_dir / 'uploads').iterdir()} == docs


def test_v8_failure_rolls_back_and_repeat_upgrade_keeps_v7(tmp_path):
    repository = Repository(tmp_path / 'docqa.db')
    migrations.MigrationHook.register(8, lambda: (_ for _ in ()).throw(RuntimeError('synthetic')))
    try:
        with pytest.raises(migrations.MigrationError):
            repository.initialize()
    finally:
        migrations.MigrationHook.clear()
    assert repository.initialize()['to'] == migrations.SCHEMA_VERSION
    assert repository.initialize()['applied'] == []
