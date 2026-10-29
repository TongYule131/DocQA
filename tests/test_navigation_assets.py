"""导航资源复用及事件循环隔离；不创建真实账号、不连接供应商。"""
import asyncio
import re
from threading import Event
from urllib.parse import urljoin

from fastapi.testclient import TestClient
import httpx

from app.config import Settings
from app.main import create_app
from app.runtime import RuntimeMismatch


def test_page_and_transitive_modules_share_immutable_source_namespace(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        fingerprint = client.app.state.runtime_guard.fingerprint
        page = client.get('/admin')
        paths = re.findall(r'(?:src|href)="(/static/[^\"]+)"', page.text)
        assert paths and all(path.startswith(f'/static/{fingerprint}/') for path in paths)
        assert page.headers['cache-control'] == 'no-store'
        pending, visited = list(paths), set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'public, max-age=31536000, immutable'
            if path.endswith('.js'):
                for relative in re.findall(r"(?:from\s*|import\s*)['\"](\./[^'\"]+)['\"]", response.text):
                    dependency = urljoin(path, relative)
                    assert dependency.startswith(f'/static/{fingerprint}/')
                    pending.append(dependency)
        assert f'/static/{fingerprint}/api-client.js' in visited
        assert f'/static/{fingerprint}/profile.js' in visited
        assert client.get('/api/auth/me').headers['cache-control'] == 'no-store'


def test_unversioned_or_unknown_assets_are_never_immutable(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        fingerprint = client.app.state.runtime_guard.fingerprint
        for path, status in [('/static/admin.js', 200), ('/static/admin.js?v=old', 200),
                             ('/static/' + '0' * 64 + '/admin.js', 404),
                             (f'/static/{fingerprint}/missing.js', 404)]:
            response = client.get(path)
            assert response.status_code == status
            assert response.headers['cache-control'] == 'no-store'


def test_static_code_does_not_read_user_session_but_still_checks_runtime(tmp_path, monkeypatch):
    with TestClient(create_app(Settings(data_dir=tmp_path, team_mode=True))) as client:
        def unexpected(*args):
            raise AssertionError('public assets must not load account state')
        monkeypatch.setattr(client.app.state.product_access, 'session', unexpected)
        path = f'/static/{client.app.state.runtime_guard.fingerprint}/auth-bridge.js'
        assert client.get(path).status_code == 200
        def mismatch():
            raise RuntimeMismatch('synthetic code update')
        monkeypatch.setattr(client.app.state.runtime_guard, 'check', mismatch)
        response = client.get(path)
        assert response.status_code == 503 and response.json()['code'] == 'runtime_mismatch'
        assert 'immutable' not in response.headers.get('cache-control', '')


def test_runtime_io_does_not_block_unrelated_requests_on_event_loop(tmp_path, monkeypatch):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        entered, release = Event(), Event()
        def slow_check():
            entered.set()
            assert release.wait(5), 'test did not release runtime check'
        monkeypatch.setattr(client.app.state.runtime_guard, 'check', slow_check)
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://isolated') as http:
                guarded = asyncio.create_task(http.get('/login'))
                try:
                    assert await asyncio.to_thread(entered.wait, 2)
                    assert not guarded.done()
                    response = await asyncio.wait_for(http.get('/unknown-navigation-probe'), 2)
                    assert response.status_code == 404
                finally:
                    release.set()
                    await guarded
        asyncio.run(exercise())
