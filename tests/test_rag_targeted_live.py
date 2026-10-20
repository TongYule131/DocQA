"""定向真实验收入口的零网络回归；生产SDK／任务／账本，HTTP边界用桩。"""
import copy
import json
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.evaluation_budget import BudgetTransport, EvaluationBudget, EvaluationStopped
from app.rag import RagService
from app.rag_evaluation import digest
from app.rag_prompts import TASK_PROMPT_VERSION, build_task_system_prompt
from app.repository import Repository, now_iso
from app.schemas import Chunk, Document, SourceLocation
from app.vector_index import DocumentIndex
from scripts import evaluate_rag_targeted_live as live
from tests.test_question_jobs import REPLY, TEXT, QUESTION


def scope(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "ROOT", tmp_path)
    directory = tmp_path / "data" / "new-stage"
    directory.mkdir(parents=True)
    (directory / "queries").mkdir()
    env = Settings(data_dir=tmp_path / "data", embedding_api_key="test-key", deepseek_api_key="test-key")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: env))
    settings = replace(env, data_dir=directory / "run-data", rag_retrieval_k=20, rag_context_k=20,
                       question_context_k=20, question_retrieval_strategy="hybrid-context")
    repository = Repository(settings.data_dir / "docqa.db")
    repository.initialize()

    class Embedding(live.CachedEmbedding):
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    index = DocumentIndex(repository, Embedding(settings))
    originals = []
    for name in ("sheet", "office", "paper"):
        repository.create(Document(id=name, filename=name + ".txt", size=100, created_at=now_iso(), format="txt", status="uploaded"))
        repository.finish_parse(name, 1, [Chunk(id=name + "-c", document_id=name, parse_version_id=name + "-v",
            page=1, order_index=0, text=TEXT, chunk_type="text", sources=[SourceLocation(format="txt", line_start=1, line_end=1)])])
        index.build(name)
        path = directory / (name + ".txt")
        path.write_text(TEXT, encoding="utf-8")
        originals.append(dict(path=str(path), sha256=live.file_hash(path)))
    cases, hashes = [], {}
    for case_id, doc_id in zip(live.TARGETS, ("sheet", "sheet", "office", "paper")):
        rag = RagService(settings, repository, index, None)
        snapshot = rag.capture_snapshot(doc_id, QUESTION)
        raw = index.search_snapshot(doc_id, QUESTION, 20, snapshot=snapshot, include_scores=True)
        path = directory / "queries" / (case_id.replace(":", "-") + ".json")
        live.save(path, dict(snapshot=snapshot, result=raw, result_sha256=digest(raw)))
        hashes[case_id] = live.file_hash(path)
        cases.append(dict(case_id=case_id, document_id=doc_id, parse_version_id=doc_id + "-v", question=QUESTION,
            chunks=[c.model_dump() for c in repository.chunks(doc_id)], required_evidence=[dict(point_id="count", alternatives=[[
                dict(chunk_id=doc_id + "-c", quote=TEXT)]])], relevant_chunk_ids=[doc_id + "-c"], expected_answerable=True))
    plan = dict(schema="docqa-rag-targeted-live-v1", targets=live.TARGETS, cases_sha256=digest(cases),
        task_prompt_version=TASK_PROMPT_VERSION, full_prompt_sha256=digest(build_task_system_prompt()),
        base_prompt_sha256=digest(build_task_system_prompt(include_table_structure=False)),
        retrieval_version=live.RETRIEVAL_STRATEGY_VERSION, query_files_sha256=hashes,
        planned_requests=dict(query=0, index=0, generation=4, total=4))
    plan["input_preview_sha256"] = digest({c["case_id"]: live.prepare(repository, settings, c,
        live.read(directory / "queries" / (c["case_id"].replace(":", "-") + ".json"))) for c in cases})
    approval = dict(authorized=True, stage="rag-targeted-v8-20261019", total_request_limit=40,
        user_reply="Synthetic approval for local regression only", plan_sha256=digest(plan),
        unchanged_provider_and_budget_config={name: getattr(settings, name) for name in live.CONFIG_FIELDS}, originals=originals)
    for name, value in (("approval.json", approval), ("plan.json", plan), ("cases.json", cases)):
        live.save(directory / name, value)
    return directory


def mock_provider(monkeypatch, *, status=200, reply=REPLY):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "synthetic failure", "type": "test"}})
        return httpx.Response(200, json={"id": "synthetic-response", "object": "chat.completion", "created": 1,
            "model": "synthetic", "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    monkeypatch.setattr(live, "BudgetTransport", lambda budget, **kwargs:
        BudgetTransport(budget, inner=httpx.MockTransport(handler), **kwargs))
    return calls


def test_plan_only_never_reads_environment_or_creates_ledger(tmp_path, monkeypatch):
    directory = scope(tmp_path, monkeypatch)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: pytest.fail("plan read environment")))
    assert live.run(directory) == dict(mode="plan_only", provider_calls_this_run=0, planned=4, limit=40)
    assert not (directory / "request-ledger.db").exists()


def test_full_sdk_worker_chain_uses_four_generations_and_rerun_is_free(tmp_path, monkeypatch):
    directory = scope(tmp_path, monkeypatch)
    calls = mock_provider(monkeypatch)
    assert live.run(directory, allow_online=True)["used"] == 4
    assert len(calls) == 4
    ledger = live.read(directory / "request-ledger.json")
    assert ledger["remaining"] == 36 and all(c["role"] == "generation" and c["status"] == "succeeded" for c in ledger["calls"])
    assert live.run(directory, allow_online=True)["used"] == 4
    assert len(calls) == 4
    assert len(list((directory / "outputs").glob("*.result.json"))) == 4


@pytest.mark.parametrize("change", ["old_approval", "limit", "case", "plan", "cache", "original", "config", "preview"])
def test_invalid_scope_cannot_send_or_create_paid_ledger(tmp_path, monkeypatch, change):
    directory = scope(tmp_path, monkeypatch)
    calls = mock_provider(monkeypatch)
    if change in {"old_approval", "limit", "config"}:
        a = live.read(directory / "approval.json")
        if change == "old_approval": a["stage"] = "old-exhausted-stage"
        if change == "limit": a["total_request_limit"] = 41
        if change == "config": a["unchanged_provider_and_budget_config"]["deepseek_model"] = "other"
        live.save(directory / "approval.json", a)
    if change == "case":
        c = live.read(directory / "cases.json"); c[0]["question"] += "改题"
        live.save(directory / "cases.json", c)
    if change == "plan":
        p = live.read(directory / "plan.json"); p["planned_requests"]["generation"] = 5
        live.save(directory / "plan.json", p)
    if change == "cache":
        path = directory / "queries/xlsx-revenue-Q2.json"
        q = live.read(path); q["result"]["results"][0]["text"] += "伪造"
        live.save(path, q)
    if change == "original": (directory / "sheet.txt").write_text("changed", encoding="utf-8")
    if change == "preview":
        p = live.read(directory / "plan.json"); p["input_preview_sha256"] = "not-the-approved-input"
        live.save(directory / "plan.json", p)
        a = live.read(directory / "approval.json"); a["plan_sha256"] = digest(p)
        live.save(directory / "approval.json", a)
    with pytest.raises(EvaluationStopped): live.run(directory, allow_online=True)
    assert not calls and not (directory / "request-ledger.db").exists()


@pytest.mark.parametrize("kind", ["http", "invalid_output"])
def test_first_failure_counts_and_stops_without_automatic_retry(tmp_path, monkeypatch, kind):
    directory = scope(tmp_path, monkeypatch)
    calls = mock_provider(monkeypatch, status=429 if kind == "http" else 200, reply="not-json")
    with pytest.raises(EvaluationStopped): live.run(directory, allow_online=True)
    assert len(calls) == 1
    ledger = live.read(directory / "request-ledger.json")
    assert ledger["used"] == 1 and ledger["remaining"] == 39
    assert live.read(directory / "state.json")["stopped"]
    with pytest.raises(EvaluationStopped): live.run(directory, allow_online=True)
    assert len(calls) == 1


@pytest.mark.parametrize("invalid", [None, "pending", "passed_target", "no_change", "changed_sources"])
def test_substantive_repair_keeps_cumulative_ledger_and_failed_targets_only(tmp_path, invalid):
    directory = tmp_path
    original = dict(approval_sha256="a", plan_sha256="p", cases_sha256="c", config={"model": "fixed"},
        sources={"doc": "frozen"}, queries={"query": "frozen"}, code_fingerprint="old")
    stage, fp, targets = live.freeze_stage(directory, original)
    budget = EvaluationBudget(directory / "request-ledger.db", limit=40, fingerprint=fp)
    seq = budget.reserve(role="generation", context=targets[0], content=b"one")
    budget.settle(seq, status="succeeded", elapsed_ms=1, http_status=200)
    review = dict(overall="not_fully_accepted", failed_case_ids=[targets[0]], remaining_case_ids=[targets[0]])
    if invalid == "pending": review["remaining_case_ids"].append(targets[1])
    live.save(directory / "semantic-review.json", review)
    repair = dict(original_freeze_sha256=digest(original), parent_stage=".", parent_review_sha256=digest(review),
        reason="Substantive condition-scope repair", same_stage_limit=40, no_ledger_reset=True, targets=review["remaining_case_ids"])
    current = copy.deepcopy(original); current["code_fingerprint"] = "new"
    if invalid == "passed_target": repair["targets"] = [targets[-1]]
    if invalid == "no_change": current["code_fingerprint"] = "old"
    if invalid == "changed_sources": current["sources"] = {"other": "source"}
    live.save(directory / "repair-v9-scope.json", repair)
    if invalid not in {None, "pending"}:
        with pytest.raises(EvaluationStopped): live.freeze_stage(directory, current, "v9-scope")
    else:
        _, reused_fp, chosen = live.freeze_stage(directory, current, "v9-scope")
        assert reused_fp == fp and chosen == review["remaining_case_ids"]
        assert EvaluationBudget(directory / "request-ledger.db", limit=40, fingerprint=reused_fp).report()["used"] == 1
