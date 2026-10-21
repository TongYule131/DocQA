"""密码长度边界覆盖初始化、创建和重设；不访问正式库或模型。"""
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.product_access import password_hash, verify_password
from app.product_routes import Login


@pytest.mark.parametrize('length', [4, 32])
def test_password_hash_accepts_inclusive_boundaries(length):
    password = '合' * length
    encoded = password_hash(password)
    assert verify_password(password, encoded)
    assert not verify_password(password + 'x', encoded)


@pytest.mark.parametrize('length', [3, 33])
def test_password_hash_rejects_outside_boundaries(length):
    with pytest.raises(HTTPException) as error:
        password_hash('x' * length)
    assert error.value.status_code == 422
    assert error.value.detail == '密码须为4～32个字符'


@pytest.mark.parametrize('length', [4, 32])
def test_setup_creation_reset_and_login_share_password_boundaries(tmp_path, length):
    app = create_app(Settings(data_dir=tmp_path))
    password = 'a' * length
    body = {'username': 'operator', 'display_name': '合成管理员', 'password': password}
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        for invalid in ['x' * 3, 'x' * 33]:
            response = client.post('/api/auth/setup', json={**body, 'password': invalid})
            assert response.status_code == 422 and invalid not in response.text
            assert client.get('/api/auth/setup-status').json()['available']
        response = client.post('/api/auth/setup', json=body)
        assert response.status_code == 201, response.text
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        customer = {**body, 'username': 'customer', 'display_name': '合成客户'}
        for invalid in ['x' * 3, 'x' * 33]:
            response = client.post('/api/admin/users', json={**customer, 'password': invalid})
            assert response.status_code == 422 and invalid not in response.text
        response = client.post('/api/admin/users', json=customer)
        assert response.status_code == 201, response.text
        user_id = response.json()['id']
        path = f'/api/admin/users/{user_id}/password'
        for invalid in ['x' * 3, 'x' * 33]:
            response = client.post(path, json={'password': invalid})
            assert response.status_code == 422 and invalid not in response.text
        # 无效重设不能更改密码；有效重设后的新密码可登录。
        with TestClient(app, base_url='http://127.0.0.1') as customer_client:
            assert customer_client.post('/api/auth/login', json=customer | {'display_name': None}).status_code == 422
            credentials = {'username': 'customer', 'password': password}
            assert customer_client.post('/api/auth/login', json=credentials).status_code == 200
            new_password = 'b' * length
            assert client.post(path, json={'password': new_password}).status_code == 200
            assert customer_client.get('/api/documents').status_code == 401
            assert customer_client.post('/api/auth/login', json=credentials).status_code == 401
            assert customer_client.post('/api/auth/login', json={**credentials, 'password': new_password}).status_code == 200


def test_login_still_accepts_legacy_password_lengths():
    assert Login(username='legacy', password='a' * 128).password == 'a' * 128


def test_password_form_limits_match_api_policy():
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.passwords = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'input' and attrs.get('type') == 'password':
                self.passwords.append(attrs)

    root = Path(__file__).resolve().parents[1] / 'app' / 'web'
    for name, count in [('setup.html', 2), ('admin.html', 1)]:
        parser = Inputs()
        source = (root / name).read_text(encoding='utf-8')
        parser.feed(source)
        assert len(parser.passwords) == count
        assert all(field['minlength'] == '4' and field['maxlength'] == '32' for field in parser.passwords)
        assert '4～32位' in source and '12～128位' not in source
