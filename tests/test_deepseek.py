"""通过 HTTP 模拟验证真实 SDK 请求与错误转换，不使用真实密钥或访问外网。"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from app.config import Settings
from app.deepseek import DeepSeekModel, ModelError
from app.main import create_app


def mock_api(monkeypatch, handler):
    # 替换客户端的 HTTP 传输层，保留 SDK 序列化和响应解析逻辑。
    def client(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr('app.deepseek.OpenAI', client)


def completion(content='连接成功', finish_reason='stop'):
    return {'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'deepseek-flash',
            'choices': [{'index': 0, 'finish_reason': finish_reason,
                         'message': {'role': 'assistant', 'content': content, 'reasoning_content': '不应返回的思考内容'}}]}


def test_request_contract_and_connection_endpoint(monkeypatch, tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert request.headers['authorization'] == 'Bearer test-secret'
        payload = json.loads(request.content)
        assert payload['model'] == 'deepseek-flash'
        assert payload['thinking'] == {'type': 'enabled'}
        assert payload['reasoning_effort'] == 'high'
        assert payload['stream'] is False
        assert payload['max_tokens'] == 8192
        assert payload['messages'][1]['content'] == '请回复：连接成功'
        return httpx.Response(200, json=completion())
    mock_api(monkeypatch, handler)
    settings = Settings(data_dir=tmp_path, deepseek_api_key='test-secret')
    with TestClient(create_app(settings)) as client:
        status = client.get('/api/model/status')
        assert status.json()['configured'] is True
        assert 'test-secret' not in status.text
        assert requests == []  # 读取状态不触发计费调用。
        response = client.post('/api/model/test')
        assert response.status_code == 200
        assert response.json()['answer'] == '连接成功'
        assert 'reasoning_content' not in response.text
        assert client.get('/api/capabilities').json()['rag'] is False
    assert len(requests) == 1


def test_missing_key_does_not_create_client(monkeypatch, tmp_path):
    def unexpected(**kwargs):
        pytest.fail('未配置密钥时不应建立客户端')
    monkeypatch.setattr('app.deepseek.OpenAI', unexpected)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get('/api/model/status').json()['configured'] is False
        assert client.post('/api/model/test').status_code == 503


@pytest.mark.parametrize('upstream,local', [(400, 502), (401, 502), (402, 502), (403, 502), (404, 502), (429, 503), (500, 502)])
def test_upstream_errors_are_sanitized(monkeypatch, tmp_path, upstream, local):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(upstream, json={'error': {'message': 'test-secret private response'}})
    mock_api(monkeypatch, handler)
    with TestClient(create_app(Settings(data_dir=tmp_path, deepseek_api_key='test-secret'))) as client:
        response = client.post('/api/model/test')
    assert response.status_code == local
    assert 'test-secret' not in response.text
    assert 'private response' not in response.text
    assert len(requests) == 1  # SDK 自动重试已关闭。


@pytest.mark.parametrize('kind,code', [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
def test_network_errors(monkeypatch, kind, code):
    def handler(request):
        raise kind('private network information', request=request)
    mock_api(monkeypatch, handler)
    with pytest.raises(ModelError) as error:
        DeepSeekModel(Settings(deepseek_api_key='test-secret')).generate('system', 'user')
    assert error.value.status_code == code
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('result', [completion('', 'stop'), completion('截断', 'length'), {'choices': []}, {'choices': None}])
def test_invalid_or_incomplete_answers(monkeypatch, result):
    mock_api(monkeypatch, lambda request: httpx.Response(200, json=result))
    with pytest.raises(ModelError) as error:
        DeepSeekModel(Settings(deepseek_api_key='test-secret')).generate('system', 'user')
    assert error.value.status_code == 502


def test_env_loading_precedence_and_secret_repr(monkeypatch, tmp_path):
    # 不更改调用进程的真实环境，避免测试相互污染。
    for key in list(os.environ):
        if key.startswith(('DEEPSEEK_', 'DOCQA_')):
            monkeypatch.delenv(key)
    env = tmp_path / '.env'
    env.write_text('DEEPSEEK_API_KEY=file-secret\nDEEPSEEK_MODEL=file-model\n', encoding='utf-8')
    monkeypatch.setenv('DEEPSEEK_MODEL', 'environment-model')
    settings = Settings.from_env(env)
    assert settings.deepseek_model == 'environment-model'
    assert settings.deepseek_api_key == 'file-secret'
    assert 'file-secret' not in repr(settings)


@pytest.mark.parametrize('kwargs', [
    {'deepseek_base_url': '[https://api.deepseek.com](https://api.deepseek.com)'},
    {'deepseek_base_url': 'http://api.deepseek.com'},
    {'deepseek_thinking': 'yes'}, {'deepseek_reasoning_effort': 'invalid'},
    {'deepseek_timeout_seconds': float('nan')}, {'deepseek_max_tokens': 0},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)
