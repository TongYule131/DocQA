"""工作区入口必须只读，并继承现有运行一致性保护。"""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.runtime import RuntimeMismatch


def test_workspace_navigation_is_read_only(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('打开页面或模块不能调用模型')
    monkeypatch.setattr('app.deepseek.DeepSeekModel.generate', unexpected)
    monkeypatch.setattr('app.embedding.APIEmbedding.embed', unexpected)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        before = (tmp_path / 'docqa.db').read_bytes()
        for path in ['/', '/app', '/single', '/knowledge', '/login', '/admin', '/admin/users', '/admin/settings', '/admin/documents', '/setup', '/tools', '/static/conversation-client.js', '/static/conversation.js', '/static/api-client.js',
                     '/static/question-client.js', '/static/answer-view.js']:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
        assert (tmp_path / 'docqa.db').read_bytes() == before


@pytest.mark.parametrize('path', ['/', '/app', '/single', '/knowledge', '/login', '/admin', '/admin/users', '/admin/settings', '/admin/documents', '/setup', '/tools'])
def test_all_workspace_pages_refuse_runtime_mismatch(tmp_path, monkeypatch, path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        def mismatch():
            raise RuntimeMismatch('请完整重启当前版本')
        monkeypatch.setattr(client.app.state.runtime_guard, 'check', mismatch)
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()['code'] == 'runtime_mismatch'
