"""任务式问答走真实SDK／索引／RAG，只在供应商HTTP边界使用MockTransport。"""
import json

import pytest

from app.config import Settings
from app.deepseek import DeepSeekModel
from app.embedding import APIEmbedding, EmbeddingError
from app.question_worker import QuestionWorker
from app.rag import RagService
from app.repository import Repository
from app.vector_index import DocumentIndex
from test_rag_api import DOC, Gateway as SyncGateway, indexed_client, seed_document
from tests.test_question_jobs import REPLY


class Gateway(SyncGateway):
    def __init__(self):
        super().__init__()
        self.generation_handler=lambda payload:self.completion(REPLY)


def test_question_job_http_worker_history_and_export_are_durable(tmp_path, monkeypatch):
    gateway = Gateway()
    gateway.install(monkeypatch)
    for client, settings in indexed_client(tmp_path, gateway):
        before = len(gateway.embedding_requests)
        payload = {"question": "全书包含多少部分？", "idempotency_key": "submit-a"}
        submitted = client.post(f"/api/documents/{DOC}/question-jobs", json=payload)
        assert submitted.status_code == 202
        job_id = submitted.json()["job"]["id"]
        assert submitted.json()["job"]["request_id"] == submitted.headers["X-Request-ID"]
        assert client.post(f"/api/documents/{DOC}/question-jobs", json=payload).json()["job"]["id"] == job_id
        assert len(gateway.embedding_requests) == before and not gateway.generation_requests
        assert QuestionWorker(settings, Repository(tmp_path / "docqa.db")).run_job(job_id)
        job = client.get(f"/api/question-jobs/{job_id}").json()
        assert job["status"] == "succeeded", job
        assert job["requests_used"] == 2
        assert [call["role"] for call in job["calls"]] == ["embedding", "generation"]
        assert "snapshot_json" not in job and "lease_token" not in job
        result = client.get(f"/api/question-results/{job['result_id']}").json()
        assert "21" in result["answer"]["answer"]
        assert result["answer"]["parse_version_id"] == job["parse_version_id"]
        for _ in range(2):
            assert client.get(f"/api/documents/{DOC}/question-jobs").json()[0]["id"] == job_id
            assert client.get(f"/api/question-results/{job['result_id']}/export?format=json").status_code == 200
            assert client.get(f"/api/question-results/{job['result_id']}/export?format=markdown").status_code == 200
        assert len(gateway.embedding_requests) == before + 1
        assert len(gateway.generation_requests) == 1
        assert client.post(f"/api/documents/{DOC}/question-jobs", json={**payload, "question": "不同问题"}).status_code == 409


def test_queued_question_keeps_old_index_when_active_version_changes(tmp_path, monkeypatch):
    gateway = Gateway()
    gateway.install(monkeypatch)
    for client, settings in indexed_client(tmp_path, gateway):
        job = client.post(f"/api/documents/{DOC}/question-jobs", json={
            "question": "全书包含多少部分？", "idempotency_key": "fixed-old"}).json()["job"]
        seed_document(tmp_path, [("new-c", "新版本全书内容分为99个部分。")], version_id="v-b")
        assert client.post(f"/api/documents/{DOC}/index").status_code == 200
        assert QuestionWorker(settings, Repository(tmp_path / "docqa.db")).run_job(job["id"])
        finished = client.get(f"/api/question-jobs/{job['id']}").json()
        assert finished["status"] == "succeeded", finished
        result = client.get(f"/api/question-results/{finished['result_id']}").json()
        assert result["is_old_version"] is True
        assert result["answer"]["index_id"] == job["index_id"]
        assert "21" in result["answer"]["answer"] and "99" not in result["answer"]["answer"]


def test_restored_retrieval_skips_embedding_and_detects_changed_locator(tmp_path, monkeypatch):
    gateway = Gateway()
    gateway.install(monkeypatch)
    for client, settings in indexed_client(tmp_path, gateway):
        repo = Repository(tmp_path / "docqa.db")
        rag = RagService(settings, repo, DocumentIndex(repo, APIEmbedding(settings)), DeepSeekModel(settings))
        snapshot = rag.capture_snapshot(DOC, "全书多少部分？")
        checkpoint = rag.index.search_snapshot(DOC, "全书多少部分？", 8, snapshot=snapshot)
        before = len(gateway.embedding_requests)
        events = []
        outcome = rag.answer(DOC, "全书多少部分？", snapshot=snapshot,
                             retrieval_checkpoint=checkpoint, observer=lambda event, _: events.append(event))
        assert outcome.diagnostics["retrieval_reused"] is True
        assert events == ["evidence_fixed", "generation_started", "validation_started"]
        assert len(gateway.embedding_requests) == before
        with repo.connect() as db:
            db.execute("UPDATE chunks SET page=99 WHERE id='c1'")
        with pytest.raises(EmbeddingError):
            rag.answer(DOC, "全书多少部分？", snapshot=snapshot, retrieval_checkpoint=checkpoint)
        assert len(gateway.embedding_requests) == before


def test_question_job_rejects_untrusted_fields_and_queries_are_free(tmp_path, monkeypatch):
    gateway = Gateway()
    gateway.install(monkeypatch)
    for client, _ in indexed_client(tmp_path, gateway):
        before = len(gateway.embedding_requests)
        for extra in ({"snapshot": {}}, {"model": "untrusted"}, {"context": "injected"}):
            assert client.post(f"/api/documents/{DOC}/question-jobs", json={
                "question": "多少部分？", "idempotency_key": "key", **extra}).status_code == 422
        assert client.post(f"/api/documents/{DOC}/question-jobs", json={"question": "多少部分？"}).status_code == 422
        assert client.get("/api/question-jobs/missing").status_code == 404
        assert client.get("/api/question-results/missing").status_code == 404
        assert client.get("/api/rag/status").json()["question_jobs"]["persists_history"] is True
        assert len(gateway.embedding_requests) == before and not gateway.generation_requests


@pytest.mark.parametrize("maximum", [0, 1, 21, True])
def test_question_budget_is_validated(maximum):
    with pytest.raises(ValueError):
        Settings(question_max_requests=maximum)
