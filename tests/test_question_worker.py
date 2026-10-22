"""真实RAG+真实任务仓储+外部边界桩；通过不代表在线语义质量验收。"""
import json

import pytest

from app.deepseek import ModelError
from app.question_jobs import QuestionJobService
from app.question_worker import QuestionWorker
from app.rag_prompts import TASK_PROMPT_VERSION
from app.repository import Repository, TaskConflict
from app.schemas import Chunk, SourceLocation
from tests.test_question_jobs import LocalRuntime, QUESTION, REPLY, TEXT, question_fixture, submit


def worker(env):
    return QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime(), worker_id="test-question-worker")


def test_real_rag_persists_citations_and_restart_history_without_calls(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    assert worker(env).run_job(job["id"])
    complete = env.service.get_job(job["id"])
    assert complete["status"] == "succeeded", complete
    assert complete["requests_used"] == 2 and complete["requests_remaining"] == 0
    assert [entry["role"] for entry in complete["calls"]] == ["embedding", "generation"]
    assert all(entry["status"] == "succeeded" for entry in complete["calls"])
    assert [entry["stage"] for entry in complete["stage_history"]] == [
        "queued", "retrieving", "evidence_ready", "generating", "validating", "saving", "done"]
    result = env.service.get_result(complete["result_id"])
    assert result["answer"]["citations"][0]["quote"] in TEXT
    assert result["answer"]["citations"][0]["parse_version_id"] == "qv1"
    assert result["answer"]["prompt_version"] == TASK_PROMPT_VERSION
    diagnostics=env.service.store.checkpoint(job["id"], "evidence")
    assert diagnostics["input_coverage_hints"]["semantic_completeness_verified"] is False
    assert env.service.store.checkpoint(job["id"], "evidence")["selected"][0]["sources"]
    assert len(env.model.calls) == len(env.embedding.calls) == 1
    env.repository = Repository(tmp_path / "docqa.db")
    restarted = QuestionJobService(env.settings, env.repository, env.rag)
    assert restarted.get_result(complete["result_id"])["answer"] == result["answer"]
    assert restarted.export(result["id"], "json")[0].find("21个部分") >= 0
    assert restarted.export(result["id"], "markdown")[0].find("全书内容分为21个部分") >= 0
    assert submit(env)[1] == 200
    assert not worker(env).run_job(job["id"])
    assert len(env.model.calls) == len(env.embedding.calls) == 1


def test_queued_question_uses_frozen_old_index_after_new_parse_and_index(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    env.repository.finish_parse("qdoc", 1, [Chunk(id="qc2", document_id="qdoc", page=1,
        text="新版本只包含其他资料。", parse_version_id="qv2", order_index=0,
        sources=[SourceLocation(format="txt", line_start=1, line_end=1)])])
    env.rag.index.build("qdoc", rebuild=True)
    env.embedding.calls.clear()
    assert worker(env).run_job(job["id"])
    complete = env.service.get_job(job["id"])
    assert complete["status"] == "succeeded", complete
    result = env.service.get_result(complete["result_id"])
    assert result["parse_version_id"] == "qv1" and result["index_id"] == job["index_id"]
    assert result["is_old_version"] and result["answer"]["is_old_version"]
    assert result["answer"]["citations"][0]["chunk_id"] == "qc1"
    assert "新版本只包含" not in env.model.calls[0][1]


def test_safe_resume_reuses_query_checkpoint_without_second_embedding(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    old = env.service.store.claim("old-worker", 60, job_id=job["id"])
    sequence = env.service.store.reserve(job["id"], old["lease_token"], "embedding")
    snapshot = json.loads(old["snapshot_json"])
    search_result = env.rag.index.search_snapshot("qdoc", QUESTION, 8, snapshot=snapshot)
    assert env.service.store.save_checkpoint(job["id"], old["lease_token"], role="retrieval",
        payload=search_result, sequence_no=sequence)
    with env.repository.connect() as db:
        db.execute("UPDATE question_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job["id"],))
    assert worker(env).run_job(job["id"])
    complete = env.service.get_job(job["id"])
    assert complete["status"] == "succeeded", complete
    assert complete["attempt_count"] == 2 and complete["requests_used"] == 2
    assert len(env.embedding.calls) == len(env.model.calls) == 1


def test_cancel_during_generation_keeps_checkpoint_but_does_not_publish(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    def cancel(system, user):
        env.service.cancel_job(job["id"])
        return REPLY
    env.model.handler = cancel
    assert worker(env).run_job(job["id"])
    cancelled = env.service.get_job(job["id"])
    assert cancelled["status"] == "cancelled" and not cancelled["result_id"]
    assert cancelled["requests_used"] == 2
    assert len(env.model.calls) == len(env.embedding.calls) == 1


def test_invalid_model_citation_is_failed_and_never_published(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    env.model.handler = lambda _s, _u: REPLY.replace('"ref": 1', '"ref": 99')
    assert worker(env).run_job(job["id"])
    failed = env.service.get_job(job["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "rag_output_invalid"
    assert not failed["result_id"]
    assert failed["calls"][-1]["status"] == "failed"
    assert not worker(env).run_job(job["id"])
    with pytest.raises(TaskConflict, match="预算"):
        env.service.retry_job(job["id"], {"idempotency_key": "retry-invalid"})


@pytest.mark.parametrize("failure", [ModelError(504, "sensitive-upstream-text"), RuntimeError("sensitive-upstream-text")])
def test_uncertain_call_never_automatically_reissues_or_leaks(tmp_path, caplog, failure):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    def fail(_s, _u):
        raise failure
    env.model.handler = fail
    assert worker(env).run_job(job["id"])
    failed = env.service.get_job(job["id"])
    assert failed["status"] == "needs_attention" and failed["error_code"] == "call_uncertain"
    assert failed["calls"][-1]["status"] == "uncertain"
    assert "sensitive-upstream-text" not in json.dumps(failed) + caplog.text
    assert not worker(env).run_job(job["id"])
    assert len(env.model.calls) == len(env.embedding.calls) == 1


def test_empty_evidence_only_uses_one_embedding_request(tmp_path):
    env = question_fixture(tmp_path, rag_min_score=1.0)
    env.embedding.handler = lambda _texts: [[0.0, 1.0]]
    job, _ = submit(env)
    assert worker(env).run_job(job["id"])
    complete = env.service.get_job(job["id"])
    assert complete["status"] == "succeeded", complete
    answer = env.service.get_result(complete["result_id"])["answer"]
    assert answer["status"] == "insufficient_evidence" and not answer["citations"]
    assert complete["requests_used"] == len(env.embedding.calls) == 1
    assert not env.model.calls


def test_lost_lease_cannot_publish_old_worker_output(tmp_path):
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    def expire(_s, _u):
        with env.repository.connect() as db:
            db.execute("UPDATE question_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job["id"],))
        return REPLY
    env.model.handler = expire
    assert worker(env).run_job(job["id"])
    assert not env.service.get_job(job["id"])["result_id"]
    assert not worker(env).run_job(job["id"])
    recovered = env.service.get_job(job["id"])
    assert recovered["status"] == "needs_attention" and recovered["requests_used"] == 2
    assert len(env.model.calls) == 1


def test_explicit_retry_reuses_retrieval_and_cumulative_budget(tmp_path):
    env = question_fixture(tmp_path, question_max_requests=4)
    job, _ = submit(env)
    env.model.handler = lambda _s, _u: "invalid-json"
    assert worker(env).run_job(job["id"])
    failed = env.service.get_job(job["id"])
    assert failed["status"] == "failed" and failed["requests_used"] == 2
    assert failed["diagnostics"]["selected"][0]["chunk_id"] == "qc1"
    assert "text" not in failed["diagnostics"]["selected"][0]
    assert env.service.store.checkpoint(job["id"], "evidence")["selected"][0]["text"] == TEXT
    env.model.handler = None
    env.service.retry_job(job["id"], {"idempotency_key": "retry-valid"})
    assert worker(env).run_job(job["id"])
    complete = env.service.get_job(job["id"])
    assert complete["status"] == "succeeded" and complete["requests_used"] == 3
    assert len(env.embedding.calls) == 1 and len(env.model.calls) == 2
    assert complete["diagnostics"]["retrieval_reused"]


def test_markdown_export_textualizes_injected_markup_in_question_and_evidence(tmp_path):
    env = question_fixture(tmp_path)
    hostile = '<img src="https://invalid.example/pixel"> ![image](https://invalid.example/pixel)\n# forged heading'
    env.repository.finish_parse("qdoc", 1, [Chunk(id="hostile", document_id="qdoc", page=1,
        text=hostile, parse_version_id="hostile-version", order_index=0,
        sources=[SourceLocation(format="txt", line_start=1, line_end=2)])])
    env.rag.index.build("qdoc", rebuild=True)
    def reply(_s, _u):
        from tests.fixtures.rag_quality.claim_protocol import upgrade
        return json.dumps(upgrade({"status": "answered", "conclusion": [{"text": '<img src="https://invalid.example/pixel">', "refs": [1],"context":[]}],
            "explanation": [], "clarification_questions": [], "evidence_quotes": [{"ref": 1, "quote": hostile}],
            "answer_parts":[{"part_id":"p1","state":"answered","fact_indices":[0],"missing_detail":""}]}, {1: hostile}, strict=True), ensure_ascii=False)
    env.model.handler = reply
    job, _ = submit(env, question="请解释以下原文：" + hostile)
    assert worker(env).run_job(job["id"])
    complete = env.service.get_job(job["id"])
    assert complete["status"] == "succeeded", complete
    markdown = env.service.export(complete["result_id"], "markdown")[0]
    assert "<img" not in markdown and "![image](" not in markdown
    assert "\n# forged heading" not in markdown
    assert "&lt;img" in markdown and "&#10;" in markdown


def test_sdk_mocktransport_full_worker_chain_and_usage_persistence(tmp_path, monkeypatch):
    from app.deepseek import DeepSeekModel
    from app.embedding import APIEmbedding
    from app.rag import RagService
    from app.vector_index import DocumentIndex
    from tests.test_rag_api import Gateway

    gateway = Gateway()
    gateway.install(monkeypatch)
    def complete(_payload):
        assert _payload['stream'] is True
        response = gateway.completion(REPLY)
        body = response.json()
        body["usage"] = {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}
        import httpx
        chunks=[dict(id='synthetic-stream',object='chat.completion.chunk',created=0,model='synthetic',
                     choices=[{'index':0,'delta':{'content':REPLY},'finish_reason':None}]),
                dict(id='synthetic-stream',object='chat.completion.chunk',created=0,model='synthetic',
                     choices=[{'index':0,'delta':{},'finish_reason':'stop'}]),
                dict(id='synthetic-stream',object='chat.completion.chunk',created=0,model='synthetic',choices=[],usage=body['usage'])]
        content=''.join('data: '+json.dumps(c,ensure_ascii=False)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
        return httpx.Response(200,content=content.encode(),headers={"x-request-id":"provider-safe-id","Content-Type":"text/event-stream"})
    gateway.generation_handler = complete
    env = question_fixture(tmp_path)
    # 外部边界使用真实SDK和适配器；初始合成索引重建到对应提供方签名。
    index = DocumentIndex(env.repository, APIEmbedding(env.settings))
    index.build("qdoc", rebuild=True)
    env.rag = RagService(env.settings, env.repository, index, DeepSeekModel(env.settings))
    env.service = QuestionJobService(env.settings, env.repository, env.rag)
    gateway.embedding_requests.clear()
    job, _ = submit(env)
    assert not gateway.embedding_requests and not gateway.generation_requests
    assert worker(env).run_job(job["id"])
    complete_job = env.service.get_job(job["id"])
    assert complete_job["status"] == "succeeded", complete_job
    assert len(gateway.embedding_requests) == len(gateway.generation_requests) == 1
    usage = complete_job["calls"][-1]["provider_usage"]
    assert usage["usage"] == {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}
    assert usage["provider_request_id"] == "provider-safe-id" and usage["finish_reason"] == "stop"
    assert complete_job["calls"][0]["provider_usage"]["calls"][0]["usage_status"] == "unknown"
    assert "不应返回的思考内容" not in json.dumps(complete_job, ensure_ascii=False)


def test_recovered_answer_checkpoint_with_forged_quote_is_not_published(tmp_path):
    env = question_fixture(tmp_path)
    first, _ = submit(env)
    assert worker(env).run_job(first["id"])
    original = env.service.get_result(env.service.get_job(first["id"])["result_id"])
    other, _ = submit(env, key="second-question")
    claimed = env.service.store.claim("checkpoint-test", 60, job_id=other["id"])
    altered = json.loads(json.dumps(original["answer"]))
    altered["citations"][0]["quote"] = "原文不存在的引述"
    assert env.service.store.save_checkpoint(other["id"], claimed["lease_token"], role="answer",
        payload=altered, sequence_no=None, diagnostics=env.service.store.checkpoint(first["id"], "evidence"))
    with env.repository.connect() as db:
        db.execute("UPDATE question_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (other["id"],))
    assert worker(env).run_job(other["id"])
    rejected = env.service.get_job(other["id"])
    assert rejected["status"] == "failed" and rejected["result_id"] is None
    assert env.service.get_result(original["id"])["answer"] == original["answer"]
    assert len(env.model.calls) == len(env.embedding.calls) == 1


def test_partial_answer_persists_and_exports_missing_points(tmp_path):
    env = question_fixture(tmp_path)
    body = json.loads(REPLY)
    body.update(status="partial", missing_points=["第二部分的实践效果没有足够依据。"])
    body["answer_parts"].append({"part_id":"p2","state":"missing_evidence",
                                  "missing_detail":body["missing_points"][0]})
    env.model.handler = lambda *_: json.dumps(body, ensure_ascii=False)
    job, _ = submit(env,question=QUESTION+"第二部分的实践效果如何？")
    assert worker(env).run_job(job["id"])
    complete = env.service.get_job(job["id"])
    assert complete["status"] == "succeeded", complete
    result = env.service.get_result(complete["result_id"])
    assert result["answer"]["status"] == "partial"
    assert result["answer"]["missing_points"] == body["missing_points"]
    assert body["missing_points"][0] in result["answer"]["answer"]
    assert body["missing_points"][0] in env.service.export(result["id"], "markdown")[0]
    from app.retrieval_planning import RETRIEVAL_STRATEGY_VERSION
    assert complete["diagnostics"]["strategy"] == RETRIEVAL_STRATEGY_VERSION
    assert complete["diagnostics"]["candidate_count"] >= complete["diagnostics"]["selected_count"]
