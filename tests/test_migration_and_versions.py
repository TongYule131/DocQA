# 数据库迁移与版本化索引测试（对应任务书 T01、T02、T16、T17）。
#
# 全部离线：使用临时目录、临时 SQLite 与模拟 embedding，不依赖 Docker、密钥或现有数据。
import json
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from app import migrations
from app.config import Settings
from app.main import create_app
from app.repository import Repository
from app.schemas import Chunk, Document

# 旧库结构：与本项目改造前的 repository.initialize 完全一致。
LEGACY_SCHEMA = """
CREATE TABLE documents (
    id TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
    created_at TEXT NOT NULL, status TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE chunks (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
    page INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE INDEX ix_chunks_document ON chunks(document_id);
CREATE TABLE embedding_indexes (
    document_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    provider_signature TEXT, source_signature TEXT,
    dimension INTEGER, chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE embedding_vectors (
    document_id TEXT NOT NULL, chunk_id TEXT PRIMARY KEY, vector TEXT NOT NULL
);
CREATE INDEX ix_vectors_document ON embedding_vectors(document_id);
"""


def legacy_source_signature(chunks: list[Chunk]) -> str:
    """旧实现的 source_signature：sha256(json(chunk.model_dump()))。

    这里刻意用独立实现（而不是导入生产函数）来计算期望值，
    以便验证“旧签名被原样保留并可用”，而不是用新代码自我印证。
    """
    import hashlib
    payload = [{"id": c.id, "document_id": c.document_id, "page": c.page, "text": c.text}
               for c in chunks]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_legacy_db(path: Path, *, provider_signature: str, document_id: str = 'legacy-doc',
                    with_vectors: bool = True, status: str = 'parsed') -> dict:
    """构造一个包含有效索引的旧库，返回期望保留的关键值。"""
    chunks = [
        Chunk(id='legacy-a', document_id=document_id, page=1, text='苹果相关内容'),
        Chunk(id='legacy-b', document_id=document_id, page=2, text='香蕉相关内容'),
    ]
    signature = legacy_source_signature(chunks)
    vectors = {'legacy-a': [1.0, 0.0], 'legacy-b': [0.0, 1.0]}
    connection = sqlite3.connect(path)
    try:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
            (document_id, 'sample.pdf', 2048, '2026-09-20T00:00:00+00:00', status, 2, 2, None))
        for chunk in chunks:
            connection.execute("INSERT INTO chunks VALUES (?,?,?,?)",
                               (chunk.id, chunk.document_id, chunk.page, chunk.text))
        connection.execute(
            "INSERT INTO embedding_indexes VALUES (?,?,?,?,?,?,?)",
            (document_id, 'indexed', provider_signature, signature, 2, 2, None))
        if with_vectors:
            for chunk_id, vector in vectors.items():
                connection.execute("INSERT INTO embedding_vectors VALUES (?,?,?)",
                                   (document_id, chunk_id, json.dumps(vector)))
        connection.commit()
    finally:
        connection.close()
    return {'chunks': chunks, 'signature': signature, 'document_id': document_id}


def mock_gateway(monkeypatch, handler):
    import app.embedding as embedding_module

    def factory(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(embedding_module, 'OpenAI', factory)


def embedding_response(vectors):
    return {'object': 'list', 'model': 'qwen3.7-text-embedding',
            'data': [{'object': 'embedding', 'index': index, 'embedding': vector}
                     for index, vector in enumerate(vectors)]}


def provider_signature_of(settings: Settings) -> str:
    from app.embedding import APIEmbedding
    return APIEmbedding(settings).signature


# ---------------------------------------------------------------------------
# T01：当前旧库升级、再次启动
# ---------------------------------------------------------------------------
def test_t01_legacy_upgrade_preserves_ids_and_keeps_index_searchable(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        texts = json.loads(request.content)['input']
        return httpx.Response(200, json=embedding_response(
            [[1.0, 0.0] if '苹果' in text else [0.0, 1.0] for text in texts]))

    mock_gateway(monkeypatch, handler)
    settings = Settings(data_dir=tmp_path, embedding_api_key='k')
    db_path = tmp_path / 'docqa.db'
    legacy = build_legacy_db(db_path, provider_signature=provider_signature_of(settings))

    with TestClient(create_app(settings)) as client:
        # 迁移只做结构升级与 legacy 版本登记，不重新解析、不调用任何 API。
        assert calls == []
        document = client.get(f"/api/documents/{legacy['document_id']}").json()
        assert document['id'] == legacy['document_id']
        assert document['status'] == 'parsed'
        assert document['active_parse_version_id'] == f"legacy-{legacy['document_id']}"
        # 分块 ID、内容、页码全部保留。
        chunks = client.get(f"/api/documents/{legacy['document_id']}/chunks").json()
        assert [chunk['id'] for chunk in chunks] == ['legacy-a', 'legacy-b']
        assert [chunk['page'] for chunk in chunks] == [1, 2]
        assert chunks[0]['text'] == '苹果相关内容'
        # 相同配置下旧索引仍然可用，并且检索使用旧版本原文。
        state = client.get(f"/api/documents/{legacy['document_id']}/index").json()
        assert state['status'] == 'indexed'
        assert state['is_legacy'] is True
        assert state['parse_version_id'] == f"legacy-{legacy['document_id']}"
        result = client.post(f"/api/documents/{legacy['document_id']}/search",
                             json={'query': '苹果', 'top_k': 1}).json()
        assert result['results'][0]['chunk_id'] == 'legacy-a'
        assert result['results'][0]['page'] == 1
        assert result['parse_version_id'] == f"legacy-{legacy['document_id']}"
        assert result['is_legacy'] is True
        # 迁移不调用 embedding：只有检索本身发起了 1 次请求。
        assert len(calls) == 1

    # 再次启动：迁移不重复执行，数据与索引状态不变。
    with TestClient(create_app(settings)) as client:
        state = client.get(f"/api/documents/{legacy['document_id']}/index").json()
        assert state['status'] == 'indexed'
        chunks_again = client.get(f"/api/documents/{legacy['document_id']}/chunks").json()
        assert [chunk['id'] for chunk in chunks_again] == ['legacy-a', 'legacy-b']
        with sqlite3.connect(db_path) as db:
            applied = [row[0] for row in db.execute("SELECT version FROM schema_migrations")]
        assert applied == list(range(1, migrations.SCHEMA_VERSION + 1))
        assert len(calls) == 1  # 第二次启动没有新增任何网关调用


def test_t01_legacy_index_incomplete_is_not_migrated_as_valid(tmp_path, monkeypatch):
    """旧索引缺少向量或维度时必须迁移为失败，不能伪装成可用索引。"""
    mock_gateway(monkeypatch, lambda request: httpx.Response(500))
    settings = Settings(data_dir=tmp_path, embedding_api_key='k')
    db_path = tmp_path / 'docqa.db'
    build_legacy_db(db_path, provider_signature=provider_signature_of(settings), with_vectors=False)
    with TestClient(create_app(settings)) as client:
        state = client.get('/api/documents/legacy-doc/index').json()
        assert state['status'] == 'failed'
        assert '不完整' in (state['error'] or '')
        # 检索必须被拒绝，而不是返回无向量的“成功”结果。
        assert client.post('/api/documents/legacy-doc/search', json={'query': '苹果'}).status_code == 409


def test_t01_legacy_index_with_other_model_is_stale(tmp_path, monkeypatch):
    """旧索引的提供方签名与当前配置不一致时仍判为过期。"""
    mock_gateway(monkeypatch, lambda request: httpx.Response(500))
    settings = Settings(data_dir=tmp_path, embedding_api_key='k')
    db_path = tmp_path / 'docqa.db'
    build_legacy_db(db_path, provider_signature='other-provider-signature')
    with TestClient(create_app(settings)) as client:
        state = client.get('/api/documents/legacy-doc/index').json()
        assert state['status'] == 'stale'
        assert client.post('/api/documents/legacy-doc/search', json={'query': '苹果'}).status_code == 409


def test_t01_legacy_interrupted_status_not_forced_to_failed(tmp_path):
    """旧库的 parsing 状态不会被启动逻辑统一改成失败（改为保留可恢复语义）。"""
    settings = Settings(data_dir=tmp_path)
    db_path = tmp_path / 'docqa.db'
    build_legacy_db(db_path, provider_signature='sig', status='parsing')
    with TestClient(create_app(settings)) as client:
        document = client.get('/api/documents/legacy-doc').json()
        # 有分块 → 建立 legacy 版本 → 内容可用；不再因中断状态把内容判为失败。
        assert document['active_parse_version_id'] == 'legacy-legacy-doc'
        assert document['status'] == 'parsed'


# ---------------------------------------------------------------------------
# T02：迁移中途异常
# ---------------------------------------------------------------------------
def test_t02_migration_failure_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    db_path = tmp_path / 'docqa.db'
    legacy = build_legacy_db(db_path, provider_signature='sig')

    def boom():
        raise RuntimeError('模拟迁移中途异常')

    migrations.MigrationHook.register(1, boom)
    try:
        with pytest.raises(migrations.MigrationError) as error:
            Repository(db_path).initialize()
        assert '原库未改动' in str(error.value)
    finally:
        migrations.MigrationHook.clear()

    # 不发布半完成结构：新契约表不存在，旧表与旧数据完整。
    # schema_migrations 表本身在事务外创建，允许存在但必须没有任何已应用版本。
    with sqlite3.connect(db_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'parse_versions' not in tables
        assert 'parse_tasks' not in tables
        assert 'blocks' not in tables
        if 'schema_migrations' in tables:
            assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2
        row = db.execute("SELECT * FROM embedding_indexes").fetchone()
        assert row is not None
    # 备份存在且可用：备份中的旧库结构与数据同样完整。
    backups = sorted((tmp_path / 'db-backups').glob('*.db'))
    assert backups, '迁移前必须生成一致性备份'
    with sqlite3.connect(backups[-1]) as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    # 不静默新建空数据库掩盖失败：应用启动同样必须失败。
    with pytest.raises(migrations.MigrationError):
        migrations.MigrationHook.register(1, boom)
        try:
            with TestClient(create_app(settings)):
                pass
        finally:
            migrations.MigrationHook.clear()
    # 清除注入后迁移可以正常完成，旧数据仍被保留。
    Repository(db_path).initialize()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == migrations.SCHEMA_VERSION
    assert legacy['document_id'] == 'legacy-doc'


def test_t02_migration_is_idempotent_on_empty_database(tmp_path):
    """空库迁移同样带版本号，重复运行不重复改写。"""
    repository = Repository(tmp_path / 'docqa.db')
    first = repository.initialize()
    assert first['applied'] == list(range(1, migrations.SCHEMA_VERSION + 1))
    second = repository.initialize()
    assert second['applied'] == []
    assert second['backup'] is None
    with sqlite3.connect(tmp_path / 'docqa.db') as db:
        assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == migrations.SCHEMA_VERSION


def test_t02_backup_uses_sqlite_backup_api_with_wal(tmp_path):
    """备份必须包含 WAL 中已提交的数据，不能只复制主库文件。"""
    settings = Settings(data_dir=tmp_path)
    db_path = tmp_path / 'docqa.db'
    build_legacy_db(db_path, provider_signature='sig')
    # 打开 WAL 并写入未 checkpoint 的数据。
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO documents VALUES ('wal-doc','w.pdf',10,'2026-09-21','uploaded',0,0,NULL)")
        connection.commit()
        backup = migrations.backup_database(db_path, tmp_path / 'db-backups')
    finally:
        connection.close()
    assert backup is not None
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM documents WHERE id='wal-doc'").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# T16：A 有索引，B 重解析失败/成功未索引
# ---------------------------------------------------------------------------
def _seed_indexed_document(client, tmp_path, monkeypatch, document_id='doc-a'):
    """用本地直接写入 + 模拟 embedding 建立“A 已解析并已索引”的起点。

    同时写入真实原件文件，使后续重新解析可以由 worker 正常读取（PDF 交给 Docling）。
    """
    from io import BytesIO

    from pypdf import PdfWriter

    upload_dir = tmp_path / 'uploads'
    upload_dir.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)
    (upload_dir / document_id).write_bytes(buffer.getvalue())
    repository = Repository(tmp_path / 'docqa.db')
    repository.create(Document(id=document_id, filename='a.pdf', size=len(buffer.getvalue()),
                               created_at='2026-09-22T00:00:00+00:00', status='uploaded', format='pdf'))
    repository.finish_parse(document_id, 2, [
        Chunk(id=f'{document_id}-1', document_id=document_id, page=1, text='苹果相关内容'),
        Chunk(id=f'{document_id}-2', document_id=document_id, page=2, text='香蕉相关内容'),
    ])
    assert client.post(f'/api/documents/{document_id}/index').status_code == 200
    return repository


def test_t16_failed_reparse_keeps_old_version_searchable(tmp_path, monkeypatch):
    from app.docling_client import DoclingError, ERROR_TASK_FAILED
    from app.parse_worker import ParseWorker
    from tests.test_parse_pipeline import FakeDocling, docling_payload, upload_pdf

    def handler(request):
        texts = json.loads(request.content)['input']
        return httpx.Response(200, json=embedding_response(
            [[1.0, 0.0] if '苹果' in text else [0.0, 1.0] for text in texts]))
    mock_gateway(monkeypatch, handler)

    with TestClient(create_app(Settings(data_dir=tmp_path, embedding_api_key='k'))) as client:
        repository = _seed_indexed_document(client, tmp_path, monkeypatch)
        document_id = 'doc-a'
        version_a = client.get(f'/api/documents/{document_id}').json()['active_parse_version_id']
        chunks_a = client.get(f'/api/documents/{document_id}/chunks').json()

        # 提交重新解析（B）并让上游失败。
        submitted = client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        assert submitted.status_code == 202
        upstream = FakeDocling()
        upstream.wait_for_result = lambda task_id, **kwargs: (_ for _ in ()).throw(
            DoclingError(ERROR_TASK_FAILED, '解析服务报告任务失败'))
        worker = ParseWorker(Settings(data_dir=tmp_path), repository=repository, client=upstream,
                             sleep=lambda _s: None)
        worker.run_forever(max_tasks=1)

        # 任务失败，但预览仍是 A，分块不变，检索继续使用 A。
        document = client.get(f'/api/documents/{document_id}').json()
        assert document['task_status'] == 'failed'
        assert document['latest_task_error']
        assert document['active_parse_version_id'] == version_a
        assert document['status'] == 'parsed'
        assert client.get(f'/api/documents/{document_id}/chunks').json() == chunks_a
        state = client.get(f'/api/documents/{document_id}/index').json()
        assert state['status'] == 'indexed'
        hit = client.post(f'/api/documents/{document_id}/search',
                          json={'query': '苹果', 'top_k': 1}).json()
        assert hit['results'][0]['chunk_id'] == 'doc-a-1'
        assert hit['is_old_version'] is False

        # B 成功未索引：预览切到 B，A 索引和历史内容继续可用。
        submitted = client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        assert submitted.status_code == 202
        changed = docling_payload(text='第二版正文内容。',
                                  extra_texts=[{"text": "第二版新增段落。", "page": 2}])
        worker = ParseWorker(Settings(data_dir=tmp_path), repository=repository,
                             client=FakeDocling(result=changed), sleep=lambda _s: None)
        worker.run_forever(max_tasks=1)
        document = client.get(f'/api/documents/{document_id}').json()
        version_b = document['active_parse_version_id']
        assert version_b != version_a, (
            f'B 必须产生新版本；任务状态={document["task_status"]}，错误={document["latest_task_error"]}')
        assert document['task_status'] == 'succeeded'
        state = client.get(f'/api/documents/{document_id}/index').json()
        assert state['status'] == 'indexed'
        assert state['parse_version_id'] == version_a
        hit = client.post(f'/api/documents/{document_id}/search', json={'query': '苹果'}).json()
        assert hit['is_old_version'] is True
        assert hit['results'][0]['chunk_id'] == 'doc-a-1'
        assert client.get(f'/api/documents/{document_id}/content?version_id={version_a}').status_code == 200


# ---------------------------------------------------------------------------
# T17：B embedding 中途失败 / 成功
# ---------------------------------------------------------------------------
def test_t17_embedding_failure_keeps_old_index_and_success_switches(tmp_path, monkeypatch):
    from app.parse_worker import ParseWorker
    from tests.test_parse_pipeline import FakeDocling, docling_payload

    calls = []

    def handler(request):
        calls.append(request)
        texts = json.loads(request.content)['input']
        if fail_next[0]:
            # 模拟中途失败：不得覆盖已成功的索引。
            return httpx.Response(429, json={'error': {'message': 'rate limited'}})
        return httpx.Response(200, json=embedding_response(
            [[1.0, 0.0] if '苹果' in text else [0.0, 1.0] for text in texts]))
    fail_next = [False]
    mock_gateway(monkeypatch, handler)

    settings = Settings(data_dir=tmp_path, embedding_api_key='k', embedding_batch_size=1)
    with TestClient(create_app(settings)) as client:
        # 先让“A 已解析并已索引”（此时允许网关成功）。
        repository = _seed_indexed_document(client, tmp_path, monkeypatch)
        document_id = 'doc-a'
        version_a = client.get(f'/api/documents/{document_id}').json()['active_parse_version_id']
        # 发布 B（内容不同）。
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        changed = docling_payload(text='第二版正文内容。',
                                  extra_texts=[{"text": "第二版新增段落。", "page": 2}])
        worker = ParseWorker(settings, repository=repository, client=FakeDocling(result=changed),
                             sleep=lambda _s: None)
        worker.run_forever(max_tasks=1)
        version_b = client.get(f'/api/documents/{document_id}').json()['active_parse_version_id']
        assert version_b != version_a
        # B 建索引失败：失败记录可见，但 A 的可用索引不变。
        fail_next[0] = True
        response = client.post(f'/api/documents/{document_id}/index')
        assert response.status_code == 503
        state = client.get(f'/api/documents/{document_id}/index').json()
        assert state['status'] == 'indexed'
        assert state['parse_version_id'] == version_a
        attempts = client.get(f'/api/documents/{document_id}/index/attempts').json()['attempts']
        assert attempts and attempts[0]['status'] == 'failed'
        # 查询本身也需要向量 API；结束构建故障注入再验证旧索引实际可查询。
        fail_next[0] = False
        hit = client.post(f'/api/documents/{document_id}/search', json={'query': '苹果'}).json()
        assert hit['is_old_version'] is True
        assert hit['results'][0]['chunk_id'] == 'doc-a-1'

        # 再次建立索引（成功）：原子切换到 B 的索引。
        fail_next[0] = False
        calls.clear()
        response = client.post(f'/api/documents/{document_id}/index')
        assert response.status_code == 200
        state = client.get(f'/api/documents/{document_id}/index').json()
        assert state['status'] == 'indexed'
        assert state['parse_version_id'] == version_b
        assert state['matches_active_version'] is True
        result = client.post(f'/api/documents/{document_id}/search',
                             json={'query': '第二版', 'top_k': 1}).json()
        assert result['parse_version_id'] == version_b
        assert result['is_old_version'] is False
        # 检索命中的文本来自 B 版本，绝不能把 A 的向量与 B 的文本拼接。
        assert '第二版' in result['results'][0]['text']
        assert all('第一段正文内容' not in hit['text'] for hit in result['results'])
