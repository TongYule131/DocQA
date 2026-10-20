"""问答持久化底座离线回归；合成原文，不访问正式 data 或真实供应商。"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app import migrations
from app.config import Settings
from app.question_jobs import QuestionJobService
from app.rag import RagService
from app.rag_context import RagError
from app.repository import Repository, TaskConflict, now_iso
from app.schemas import Chunk, Document, SourceLocation
from app.vector_index import DocumentIndex


TEXT = "全书内容分为21个部分，第一部分为总述。"
QUESTION = "全书内容分为多少个部分？"
REPLY = json.dumps({"status": "answered", "conclusion": [{"text": "全书内容分为21个部分。", "refs": [1],
    "context":[{"kind":"unit","text":"个部分","ref":1}]}],
    "explanation": [], "clarification_questions": [],
    "evidence_quotes": [{"ref": 1, "quote": "全书内容分为21个部分"}],
    "answer_parts":[{"part_id":"p1","state":"answered","fact_indices":[0],"missing_detail":""}]}, ensure_ascii=False)


class Embedding:
    signature = "synthetic-embedding-signature"

    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.handler = None

    def embed(self, texts):
        self.calls.append(texts)
        if self.handler:
            return self.handler(texts)
        return [[1.0, 0.0] for _ in texts]


class Model:
    def __init__(self):
        self.calls = []
        self.handler = None

    def generate(self, system, user):
        self.calls.append((system, user))
        if self.handler:
            return self.handler(system, user)
        return REPLY


class LocalRuntime:
    """仅绕过代码热变更探测，业务和SQLite校验仍走真实生产路径。"""
    def check(self):
        return {}


def question_fixture(tmp_path, **overrides):
    overrides.setdefault("question_max_requests", 2)
    settings = Settings(data_dir=tmp_path, embedding_api_key="test-embedding-key",
                        deepseek_api_key="test-generation-key", **overrides)
    repository = Repository(tmp_path / "docqa.db")
    repository.initialize()
    repository.create(Document(id="qdoc", filename="合成问答样本.txt", size=200,
                               created_at=now_iso(), status="uploaded", format="txt"))
    repository.finish_parse("qdoc", 1, [Chunk(id="qc1", document_id="qdoc", page=1,
        text=TEXT, parse_version_id="qv1", order_index=0, chunk_type="text",
        sources=[SourceLocation(format="txt", line_start=1, line_end=1)])])
    embedding, model = Embedding(settings), Model()
    index = DocumentIndex(repository, embedding)
    index.build("qdoc")
    embedding.calls.clear()
    rag = RagService(settings, repository, index, model)
    service = QuestionJobService(settings, repository, rag)
    return SimpleNamespace(settings=settings, repository=repository, embedding=embedding,
                           model=model, rag=rag, service=service)


def submit(env, key="submit-one", question=QUESTION):
    response, status = env.service.submit("qdoc", {"question": question, "idempotency_key": key})
    return response["job"], status


def test_submit_and_poll_are_zero_call_and_freeze_snapshot(tmp_path):
    env = question_fixture(tmp_path)
    job, status = submit(env)
    assert status == 202 and job["status"] == "queued"
    assert job["parse_version_id"] == "qv1" and job["index_id"]
    assert job["request_id"] and job["max_requests"] == job["request_upper_bound"] == 2
    assert job["question"] == QUESTION and job["idempotency_key"] == "submit-one"
    assert env.service.get_job(job["id"]) == job
    assert env.service.list_jobs("qdoc")[0]["id"] == job["id"]
    snapshot = json.loads(env.service.store.row(job["id"])["snapshot_json"])
    assert snapshot["source_signature"] and snapshot["evidence_signature"]
    assert not env.model.calls and not env.embedding.calls
    assert "snapshot_json" not in job and "lease_token" not in job


def test_concurrent_submit_same_key_reuses_one_job(tmp_path):
    env = question_fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: submit(env)[0]["id"], range(2)))
    assert jobs[0] == jobs[1]
    assert len(env.service.list_jobs("qdoc")) == 1
    with pytest.raises(TaskConflict):
        submit(env, question="其他问题")
    assert not env.embedding.calls and not env.model.calls


def test_idempotency_reuses_snapshot_after_active_index_changes(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    with env.repository.connect() as db:
        db.execute("UPDATE documents SET active_index_id=NULL WHERE id='qdoc'")
        db.execute("UPDATE embedding_indexes SET is_active=0 WHERE document_id='qdoc'")
    repeated, status = submit(env)
    assert status == 202 and repeated["id"] == job["id"]
    with pytest.raises(RagError):
        submit(env, key="new-key")
    assert not env.model.calls and not env.embedding.calls


def test_claim_and_budget_are_atomic_and_fenced(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda n: env.service.store.claim(f"w{n}", 60, job_id=job["id"]), range(2)))
    assert sum(value is not None for value in claimed) == 1
    row = next(value for value in claimed if value)
    store, token = env.service.store, row["lease_token"]
    assert store.reserve(job["id"], "wrong-token", "embedding") is None
    number = store.reserve(job["id"], token, "embedding")
    assert number == 1
    assert store.reserve(job["id"], token, "generation") is None
    assert store.save_checkpoint(job["id"], token, role="retrieval", payload={"synthetic": True}, sequence_no=number)
    assert store.reserve(job["id"], token, "generation") == 2
    assert store.finish(job["id"], token, status="failed", code="synthetic", message="合成失败")
    assert env.service.get_job(job["id"])["requests_used"] == 2
    with pytest.raises(TaskConflict, match="预算"):
        env.service.retry_job(job["id"], {"idempotency_key": "retry-one"})


def test_expired_inflight_call_needs_attention_and_does_not_requeue(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    store = env.service.store
    claimed = store.claim("old-worker", 60, job_id=job["id"])
    assert store.reserve(job["id"], claimed["lease_token"], "embedding") == 1
    with env.repository.connect() as db:
        db.execute("UPDATE question_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job["id"],))
    assert store.claim("new-worker", 60) is None
    recovered = env.service.get_job(job["id"])
    assert recovered["status"] == "needs_attention" and recovered["error_code"] == "call_uncertain"
    assert recovered["calls"][0]["status"] == "uncertain"
    assert recovered["requests_used"] == 1
    assert not store.renew(job["id"], claimed["lease_token"], 60)
    assert not store.save_checkpoint(job["id"], claimed["lease_token"], role="retrieval", payload={}, sequence_no=1)


def test_cancel_queued_is_idempotent_and_does_not_call_provider(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    cancelled = env.service.cancel_job(job["id"])
    assert cancelled["status"] == "cancelled"
    assert env.service.cancel_job(job["id"])["status"] == "cancelled"
    assert env.service.store.claim("worker", 60) is None
    assert not env.embedding.calls and not env.model.calls


def test_retry_key_does_not_revive_later_failure_or_reset_budget(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    claimed = env.service.store.claim("first", 60, job_id=job["id"])
    env.service.store.finish(job["id"], claimed["lease_token"], status="failed", code="local", message="本地失败")
    retried = env.service.retry_job(job["id"], {"idempotency_key": "retry-once"})
    assert retried["status"] == "queued" and retried["last_retry_key"] == "retry-once"
    second = env.service.store.claim("second", 60, job_id=job["id"])
    env.service.store.finish(job["id"], second["lease_token"], status="failed", code="local", message="本地失败")
    again = env.service.retry_job(job["id"], {"idempotency_key": "retry-once"})
    assert again["status"] == "failed" and again["attempt_count"] == 2
    with pytest.raises(TaskConflict):
        submit(env, key="retry-once")


def test_v5_migration_from_v4_preserves_old_tables_and_rolls_back(tmp_path):
    path = tmp_path / "migration.db"
    with sqlite3.connect(path, isolation_level=None) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for version in range(1, 5):
            migrations.MIGRATIONS[version](db)
            db.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, now_iso()))
        db.execute("INSERT INTO documents(id,filename,size,created_at,status) VALUES ('keep','保留.txt',1,?,'uploaded')", (now_iso(),))
        migrations.MigrationHook.register(5, lambda: (_ for _ in ()).throw(RuntimeError("合成迁移失败")))
        try:
            with pytest.raises(migrations.MigrationError):
                migrations.run_migrations(db)
        finally:
            migrations.MigrationHook.clear()
        assert migrations.detect_version(db) == 4
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='question_jobs'").fetchone() is None
        assert db.execute("SELECT filename FROM documents WHERE id='keep'").fetchone()[0] == "保留.txt"
        migrations.run_migrations(db)
        assert migrations.validate_schema(db) == 5
        assert db.execute("SELECT filename FROM documents WHERE id='keep'").fetchone()[0] == "保留.txt"
