"""Embedding 与索引回归测试：仅使用模拟传输，不读取真实密钥或发送文档。"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from app.config import Settings
from app.embedding import APIEmbedding, EmbeddingError
from app.main import create_app
from app.repository import Repository
from app.schemas import Chunk, Document


def mock_gateway(monkeypatch, handler):
    def factory(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr('app.embedding.OpenAI', factory)


def response(vectors):
    return {'object': 'list', 'model': 'qwen3.7-text-embedding',
            'data': [{'object': 'embedding', 'index': i, 'embedding': v} for i, v in enumerate(vectors)]}


def settings(tmp_path, **kwargs):
    return Settings(data_dir=tmp_path, embedding_api_key='gateway-test-key', **kwargs)


def test_batching_order_and_gateway_contract(monkeypatch, tmp_path):
    calls = []
    def handler(request):
        assert str(request.url) == 'https://tokendance.space/gateway/v1/embeddings'
        assert request.headers['authorization'] == 'Bearer gateway-test-key'
        payload = json.loads(request.content)
        assert payload['model'] == 'qwen3.7-text-embedding'
        assert payload['encoding_format'] == 'float'
        calls.append(payload['input'])
        result = response([[int(text), 1] for text in payload['input']])
        result['data'].reverse()  # 网关乱序返回时仍须与输入顺序对应。
        return httpx.Response(200, json=result)
    mock_gateway(monkeypatch, handler)
    vectors = APIEmbedding(settings(tmp_path, embedding_batch_size=2)).embed(['1', '2', '3'])
    assert calls == [['1', '2'], ['3']]
    assert [v[0] / v[1] for v in vectors] == pytest.approx([1, 2, 3])
    assert sum(v * v for v in vectors[0]) == pytest.approx(1)


@pytest.mark.parametrize('data', [
    [], [{'index': 1, 'embedding': [1, 2]}],
    [{'index': 0, 'embedding': []}], [{'index': 0, 'embedding': [0, 0]}],
    [{'index': 0, 'embedding': ['bad', 0]}], [{'index': 0, 'embedding': None}],
])
def test_invalid_vectors(monkeypatch, tmp_path, data):
    mock_gateway(monkeypatch, lambda request: httpx.Response(200, json={'data': data}))
    with pytest.raises(EmbeddingError) as error:
        APIEmbedding(settings(tmp_path)).embed(['text'])
    assert error.value.status_code == 502


def test_duplicate_indexes_and_dimension_drift(monkeypatch, tmp_path):
    mock_gateway(monkeypatch, lambda request: httpx.Response(200, json={'data': [
        {'index': 0, 'embedding': [1, 0]}, {'index': 0, 'embedding': [0, 1]}]}))
    with pytest.raises(EmbeddingError):
        APIEmbedding(settings(tmp_path)).embed(['a', 'b'])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response([[1, 0] if len(calls) == 1 else [1, 0, 0]]))
    mock_gateway(monkeypatch, handler)
    with pytest.raises(EmbeddingError, match='维度'):
        APIEmbedding(settings(tmp_path, embedding_batch_size=1)).embed(['a', 'b'])


@pytest.mark.parametrize('upstream,local', [(401, 502), (402, 502), (403, 502), (404, 502), (429, 503), (500, 502)])
def test_safe_errors_without_retries(monkeypatch, tmp_path, upstream, local):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(upstream, json={'error': {'message': 'gateway-test-key private upstream data'}})
    mock_gateway(monkeypatch, handler)
    with TestClient(create_app(settings(tmp_path))) as client:
        result = client.post('/api/embedding/test')
    assert result.status_code == local
    assert 'gateway-test-key' not in result.text
    assert 'private upstream data' not in result.text
    assert len(calls) == 1


@pytest.mark.parametrize('kind,code', [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
def test_network_errors(monkeypatch, tmp_path, kind, code):
    def handler(request):
        raise kind('private detail', request=request)
    mock_gateway(monkeypatch, handler)
    with pytest.raises(EmbeddingError) as error:
        APIEmbedding(settings(tmp_path)).embed(['text'])
    assert error.value.status_code == code
    assert 'private' not in str(error.value)


def seed(repository, document_id):
    # 不依赖 PDF 布局，用带页码的两个分块验证索引与文档隔离。
    repository.create(Document(id=document_id, filename='sample.txt', size=30,
                               created_at='2026-09-22', status='uploaded'))
    repository.finish_parse(document_id, 2, [
        Chunk(id=document_id + '-a', document_id=document_id, page=1, text='苹果相关内容'),
        Chunk(id=document_id + '-b', document_id=document_id, page=2, text='香蕉相关内容'),
    ])


def test_index_persistence_search_isolation_and_rebuild(monkeypatch, tmp_path):
    calls = []
    def handler(request):
        texts = json.loads(request.content)['input']
        calls.append(texts)
        return httpx.Response(200, json=response([[1, 0] if '苹果' in text else [0, 1] for text in texts]))
    mock_gateway(monkeypatch, handler)
    config = settings(tmp_path)
    with TestClient(create_app(config)) as client:
        repository = Repository(tmp_path / 'docqa.db')
        seed(repository, 'doc1')
        seed(repository, 'doc2')
        assert client.get('/api/embedding/status').json()['configured'] is True
        assert calls == []
        assert client.get('/api/documents/doc1/index').json()['status'] == 'pending'
        assert client.post('/api/documents/doc1/search', json={'query': '苹果'}).status_code == 409
        assert calls == []
        result = client.post('/api/documents/doc1/index')
        assert result.json()['dimension'] == 2
        assert result.json()['chunk_count'] == 2
        assert client.post('/api/documents/doc1/index').json() == result.json()
        assert len(calls) == 1  # 重复建索引不重复计费。
        assert client.post('/api/documents/doc2/index').status_code == 200
        search = client.post('/api/documents/doc1/search', json={'query': '香蕉', 'top_k': 1}).json()
        hit = search['results'][0]
        assert hit['document_id'] == 'doc1' and hit['page'] == 2
        assert hit['score'] == pytest.approx(1)
    with TestClient(create_app(config)) as client:
        assert client.get('/api/documents/doc1/index').json()['status'] == 'indexed'
        assert client.post('/api/documents/doc1/search', json={'query': '苹果'}).json()['results'][0]['page'] == 1
        count = len(calls)
        assert client.post('/api/documents/doc1/index?rebuild=true').status_code == 200
        assert len(calls) == count + 1


def test_failed_batch_is_atomic_and_retryable(monkeypatch, tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(429, json={'error': {'message': 'private'}})
        return httpx.Response(200, json=response([[1, 0]]))
    mock_gateway(monkeypatch, handler)
    with TestClient(create_app(settings(tmp_path, embedding_batch_size=1))) as client:
        repository = Repository(tmp_path / 'docqa.db')
        seed(repository, 'doc')
        assert client.post('/api/documents/doc/index').status_code == 503
        assert client.get('/api/documents/doc/index').json()['status'] == 'failed'
        with repository.connect() as db:
            assert db.execute('SELECT count(*) FROM embedding_vectors').fetchone()[0] == 0
        assert client.post('/api/documents/doc/search', json={'query': '苹果'}).status_code == 409
        assert client.post('/api/documents/doc/index').status_code == 200


def test_configuration_changes_invalidate_index(monkeypatch, tmp_path):
    mock_gateway(monkeypatch, lambda request: httpx.Response(200, json=response([[1, 0], [0, 1]])))
    with TestClient(create_app(settings(tmp_path))) as client:
        seed(Repository(tmp_path / 'docqa.db'), 'doc')
        assert client.post('/api/documents/doc/index').status_code == 200
    with TestClient(create_app(settings(tmp_path, embedding_model='different-model'))) as client:
        assert client.get('/api/documents/doc/index').json()['status'] == 'stale'
        assert client.post('/api/documents/doc/search', json={'query': '苹果'}).status_code == 409


def test_missing_key_validation_and_interrupted_index(monkeypatch, tmp_path):
    def unexpected(**kwargs):
        pytest.fail('不应发起外网调用')
    monkeypatch.setattr('app.embedding.OpenAI', unexpected)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.post('/api/embedding/test').status_code == 503
        assert client.get('/api/documents/missing/index').status_code == 404
        repository = Repository(tmp_path / 'docqa.db')
        seed(repository, 'doc')
        assert client.post('/api/documents/doc/index').status_code == 503
        assert client.post('/api/documents/doc/search', json={'query': '  '}).status_code == 422
        assert client.post('/api/documents/doc/search', json={'query': '苹果', 'top_k': 100}).status_code == 422
        with repository.connect() as db:
            db.execute("INSERT INTO embedding_indexes(document_id,status) VALUES ('doc','indexing')")
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get('/api/documents/doc/index').json()['status'] == 'failed'


def test_env_key_is_separate_and_hidden(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(('EMBEDDING_', 'DEEPSEEK_', 'DOCQA_')):
            monkeypatch.delenv(key)
    path = tmp_path / '.env'
    path.write_text('DEEPSEEK_API_KEY=deepseek-only\nEMBEDDING_API_KEY=gateway-only\n', encoding='utf-8')
    config = Settings.from_env(path)
    assert config.embedding_api_key == 'gateway-only'
    assert 'gateway-only' not in repr(config)
    monkeypatch.setenv('EMBEDDING_API_KEY', 'environment-key')
    assert Settings.from_env(path).embedding_api_key == 'environment-key'
    path.write_text('DEEPSEEK_API_KEY=deepseek-only\n', encoding='utf-8')
    monkeypatch.delenv('EMBEDDING_API_KEY')
    assert Settings.from_env(path).embedding_api_key == ''


def test_query_dimension_and_corrupt_storage(monkeypatch, tmp_path):
    # 查询维度漂移和数据库损坏都必须拒绝检索，不能给出无意义的排序结果。
    dimension = 2
    def handler(request):
        count = len(json.loads(request.content)['input'])
        return httpx.Response(200, json=response([[1] + [0] * (dimension - 1)] * count))
    mock_gateway(monkeypatch, handler)
    with TestClient(create_app(settings(tmp_path))) as client:
        repository = Repository(tmp_path / 'docqa.db')
        seed(repository, 'doc')
        assert client.post('/api/documents/doc/index').status_code == 200
        dimension = 3
        assert client.post('/api/documents/doc/search', json={'query': '苹果'}).status_code == 409
        dimension = 2
        with repository.connect() as db:
            db.execute("UPDATE embedding_vectors SET vector='[0,0]' WHERE chunk_id='doc-a'")
        assert client.post('/api/documents/doc/search', json={'query': '苹果'}).status_code == 409
        repository.finish_parse('doc', 1, [Chunk(id='new', document_id='doc', page=1, text='新的内容')])
        assert client.get('/api/documents/doc/index').json()['status'] == 'pending'
        with repository.connect() as db:
            assert db.execute('SELECT count(*) FROM embedding_vectors').fetchone()[0] == 0


def test_busy_index_is_not_claimed_twice(monkeypatch, tmp_path):
    def unexpected(**kwargs):
        pytest.fail('已有任务时不得再次调用网关')
    monkeypatch.setattr('app.embedding.OpenAI', unexpected)
    with TestClient(create_app(settings(tmp_path))) as client:
        repository = Repository(tmp_path / 'docqa.db')
        seed(repository, 'doc')
        with repository.connect() as db:
            db.execute("INSERT INTO embedding_indexes(document_id,status) VALUES ('doc','indexing')")
        assert client.post('/api/documents/doc/index').status_code == 409
