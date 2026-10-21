"""后台的安全边界及首次设置持久化；所有模型均为离线桩。"""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.admin_routes import safe_endpoint
from app.main import create_app
from app.product_access import COOKIE, ProductAccess
from tests.test_product_access import team, publish, PASSWORD
from tests.test_question_jobs import question_fixture, Embedding, Model, QUESTION


SETUP = {'username': 'operator', 'display_name': '合成管理员', 'password': PASSWORD}


def test_local_first_setup_activates_auth_atomically_and_survives_restart(tmp_path):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        assert client.get('/api/auth/setup-status').json()['available']
        assert client.get('/admin').status_code == 200
        assert client.post('/api/admin/users', json={**SETUP, 'role': 'customer'}).status_code == 409
        response = client.post('/api/auth/setup', json=SETUP, headers={'Origin': 'http://127.0.0.1'})
        assert response.status_code == 201, response.text
        assert response.json()['user']['role'] == 'admin'
        assert 'httponly' in response.headers['set-cookie'].lower()
        assert app.state.product_access.team_mode
        assert not app.state.product_access.cookie_secure
        assert client.get('/api/auth/me').json()['user']['username'] == 'operator'
        assert client.get('/admin/users').status_code == 200
        assert client.get('/api/auth/setup-status').json()['available'] is False
        # 激活后的变更请求必须有 CSRF，包括管理员本人。
        assert client.post('/api/knowledge-bases', json={'name': '伪请求'}).status_code == 403
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        assert client.post('/api/auth/setup', json=SETUP).status_code == 409
        assert client.post('/api/auth/logout').status_code == 200
        assert client.get('/api/admin/users').status_code == 401
    with TestClient(create_app(settings), base_url='http://127.0.0.1', client=('127.0.0.1', 50001)) as client:
        assert client.get('/api/auth/me').json()['team_mode']
        assert client.get('/admin', follow_redirects=False).status_code == 303
        assert client.post('/api/auth/login', json={'username': 'operator', 'password': PASSWORD}).status_code == 200


@pytest.mark.parametrize('peer,host,headers', [
    ('192.168.1.5', 'http://127.0.0.1', {}),
    ('127.0.0.1', 'http://evil.example', {}),
    ('127.0.0.1', 'http://127.0.0.1', {'Origin': 'https://evil.example'}),
    ('127.0.0.1', 'http://127.0.0.1', {'Sec-Fetch-Site': 'cross-site'}),
])
def test_initial_setup_rejects_remote_rebinding_and_csrf(tmp_path, peer, host, headers):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, base_url=host, client=(peer, 50000)) as client:
        assert client.post('/api/auth/setup', json=SETUP, headers=headers).status_code == 403
        with app.state.product_access.repository.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM product_users').fetchone()[0] == 0
            assert db.execute('SELECT team_enabled FROM product_installation').fetchone()[0] == 0


def test_parallel_initial_setup_only_creates_one_admin(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        def submit(i):
            return client.post('/api/auth/setup', json={**SETUP, 'username': f'operator{i}'}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        assert sorted(results) == [201, 409]


def test_setup_keeps_local_conversations_and_temporary_files_private_to_first_admin(tmp_path, monkeypatch):
    env = question_fixture(tmp_path)
    monkeypatch.setattr('app.main.APIEmbedding', Embedding)
    monkeypatch.setattr('app.main.DeepSeekModel', lambda settings: Model())
    with TestClient(create_app(env.settings), base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        conversation = client.post('/api/conversations', json={'document_id': 'qdoc', 'idempotency_key': 'before-setup'}).json()
        job = client.post(f'/api/conversations/{conversation["id"]}/turns', json={'question': QUESTION, 'idempotency_key': 'before-setup-turn'}).json()['job']
        temporary = client.post('/api/conversations/temporary', headers={'Idempotency-Key': 'local-temporary'}, files={'file': ('local.txt', b'Local synthetic text')}).json()
        response = client.post('/api/auth/setup', json=SETUP)
        assert response.status_code == 201
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        assert len(client.get('/api/conversations').json()) == 2
        assert client.get(f'/api/question-jobs/{job["id"]}').status_code == 200
        assert client.get(f'/api/documents/{temporary["document_id"]}/original').status_code == 200
        assert client.post('/api/admin/users', json={'username': 'newcustomer', 'display_name': '新客户', 'password': PASSWORD}).status_code == 201
        client.post('/api/auth/logout')
        response = client.post('/api/auth/login', json={'username': 'newcustomer', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        assert client.get('/api/conversations').json() == []
        assert client.get(f'/api/conversations/{conversation["id"]}').status_code == 404
        assert client.get(f'/api/documents/{temporary["document_id"]}/original').status_code == 404
        assert not env.embedding.calls and not env.model.calls


@pytest.mark.parametrize('path', ['/admin/users', '/admin/settings', '/admin/documents', '/api/admin/overview', '/api/admin/settings'])
def test_new_admin_entries_deny_customers_and_anonymous(team, path):
    env, _, clients = team
    assert clients['alice'].get(path).status_code == 403
    response = clients['anonymous'].get(path, follow_redirects=False)
    assert response.status_code == (401 if path.startswith('/api/') else 303)
    assert clients['admin'].get(path).status_code == 200
    assert not env.embedding.calls and not env.model.calls


def test_dashboard_edit_and_empty_delete_preserve_published_sources(team):
    env, users, clients = team
    admin = clients['admin']; base = publish(clients, users)
    overview = admin.get('/api/admin/overview').json()
    assert overview['stats'] == {'knowledge_bases': 1, 'documents': 1, 'populated_bases': 1, 'creators': 1, 'users': 3}
    assert overview['bases'][0]['document_count'] == 1
    response = admin.patch(f'/api/knowledge-bases/{base["id"]}', json={'name': '更新名称', 'description': '用途'})
    assert response.status_code == 200 and response.json()['name'] == '更新名称'
    assert admin.delete(f'/api/knowledge-bases/{base["id"]}').status_code == 409
    empty = admin.post('/api/knowledge-bases', json={'name': '空草稿'}).json()
    assert admin.delete(f'/api/knowledge-bases/{empty["id"]}').status_code == 200
    assert admin.get(f'/api/knowledge-bases/{empty["id"]}').status_code == 404
    assert len(clients['alice'].get('/api/documents').json()) == 1
    assert not env.embedding.calls and not env.model.calls


def test_password_reset_revokes_all_old_sessions_and_hides_password_errors(team):
    _, users, clients = team; admin = clients['admin']; alice = clients['alice']
    path = f'/api/admin/users/{users["alice"]["id"]}/password'
    new_password = 'Changed-synthetic-password-only'
    assert alice.post(path, json={'password': new_password}).status_code == 403
    response = admin.post(path, json={'password': new_password})
    assert response.status_code == 200 and new_password not in response.text
    assert alice.get('/api/documents').status_code == 401
    assert alice.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD}).status_code == 401
    assert alice.post('/api/auth/login', json={'username': 'alice', 'password': new_password}).status_code == 200
    response = admin.post(path, json={'password': new_password, 'unexpected': new_password})
    assert response.status_code == 422 and new_password not in response.text


def test_settings_never_disclose_configured_credentials_or_auto_call(tmp_path, monkeypatch):
    def no_call(*args, **kwargs):
        pytest.fail('查看配置不得连接供应商')
    monkeypatch.setattr('app.deepseek.DeepSeekModel.generate', no_call)
    monkeypatch.setattr('app.embedding.APIEmbedding.embed', no_call)
    settings = Settings(data_dir=tmp_path, deepseek_api_key='hidden-generation-key', embedding_api_key='hidden-embedding-key',
        deepseek_base_url='https://example.invalid/private-token',
        embedding_base_url='https://vector.invalid/private-secret')
    with TestClient(create_app(settings)) as client:
        response = client.get('/api/admin/settings')
        assert response.status_code == 200
        for secret in ['hidden-generation-key', 'hidden-embedding-key', 'private-token', 'hidden-url-key', 'user:', 'other-secret', 'private-secret']:
            assert secret not in response.text
        assert response.json()['generation']['endpoint'] == 'https://example.invalid'
        assert response.json()['embedding']['configured']
    assert safe_endpoint('https://user:secret@example.invalid/private-token?key=hidden-url-key') == 'https://example.invalid'
