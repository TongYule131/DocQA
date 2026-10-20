"""付费入口的离线护栏；测试不授权真实调用。"""
import copy
import json
from pathlib import Path
import runpy

import pytest

from app.config import Settings
from app.evaluation_budget import EvaluationStopped
from app.evaluation_budget import EvaluationBudget
from app.rag_evaluation import digest
from app.rag_prompts import TASK_PROMPT_VERSION, build_task_system_prompt
from app.retrieval_planning import RETRIEVAL_STRATEGY_VERSION
from app.schemas import Chunk
from scripts import evaluate_rag_quality_live as live


def scope(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "ROOT", tmp_path)
    directory = tmp_path / "data" / "isolated"
    directory.mkdir(parents=True)
    cases = [copy.deepcopy(c) for c in runpy.run_path(str(Path(__file__).parent / "fixtures/rag_quality/golden.py"))["make_cases"]()[:7]]
    # 入口的七题三文档范围单独构造；事实仍来自各自已有版本。
    for n, case in enumerate(cases):
        case["document_id"] = f"doc-{n % 3}"
        for chunk in case["chunks"]:
            chunk["document_id"] = case["document_id"]
    plan = {"schema": "docqa-rag-live-plan-v1", "cases_sha256": digest(cases),
            "branches": ["legacy", "current", "oracle"], "task_prompt_version": TASK_PROMPT_VERSION,
            "retrieval_version": RETRIEVAL_STRATEGY_VERSION,
            "system_prompt_sha256": digest(build_task_system_prompt()),
            "planned_requests": {"index": 0, "query": 7, "generation": 21, "total": 28}}
    approval = {"authorized": True, "total_request_limit": 40, "plan_sha256": digest(plan)}
    for name, content in (("cases.json", cases), ("plan.json", plan), ("approval.json", approval)):
        live.save(directory / name, content)
    return directory


def test_plan_only_does_not_create_database_or_ledger(tmp_path, monkeypatch):
    directory = scope(tmp_path, monkeypatch)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_dir=tmp_path / "data")))
    assert live.run(directory) == {"mode": "plan_only", "provider_calls_this_run": 0, "planned": 28, "limit": 40}
    assert not (directory / "request-ledger.db").exists()
    assert not (directory / "run-data").exists()


@pytest.mark.parametrize("target", ["cases.json", "plan.json", "approval.json"])
def test_scope_tampering_cannot_spend(tmp_path, monkeypatch, target):
    directory = scope(tmp_path, monkeypatch)
    value = live.read(directory / target)
    if target == "cases.json": value[0]["question"] += "篡改"
    elif target == "plan.json": value["planned_requests"]["total"] = 27
    else: value["authorized"] = False
    live.save(directory / target, value)
    with pytest.raises(EvaluationStopped):
        live.load_scope(directory, Settings(data_dir=tmp_path / "data"))
    assert not (directory / "request-ledger.db").exists()


def test_oracle_uses_frozen_extra_context_actual_scores_and_keeps_query_unchanged():
    case = runpy.run_path(str(Path(__file__).parent / "fixtures/rag_quality/golden.py"))["make_cases"]()[0]
    case["oracle_context_chunk_ids"] = [case["chunks"][-1]["id"]]
    raw = {**case["vector_result"], "candidate_scores": {c["id"]: 0.15 for c in case["chunks"]}}
    before = copy.deepcopy(raw)
    result = live.branch_retrieval(case, "oracle", raw, [Chunk(**c) for c in case["chunks"]], 12000)
    assert raw == before
    assert case["chunks"][-1]["id"] in result["evidence_order"]
    assert all(h["score"] == 0.15 for h in result["results"])
    assert result["evaluation_branch"] == "oracle"


@pytest.mark.parametrize("drift", [None, "config", "cases_sha256", "sources"])
def test_repair_inherits_original_budget_and_keeps_failure(tmp_path, drift, monkeypatch):
    monkeypatch.setattr(live,"TASK_PROMPT_VERSION","rag-task-v5")
    original = {"approval_sha256": "approval", "plan_sha256": "plan", "cases_sha256": "cases",
                "config": {"model": "unchanged"}, "sources": {"doc": "fixed"}, "code_fingerprint": "old"}
    live.save(tmp_path / "runtime-freeze.json", original)
    stopped = {"stopped": {"error_type": "EvaluationStopped", "reason": "first failure"}}
    live.save(tmp_path / "state.json", stopped)
    live.save(tmp_path / "repair-prompt-v5.json", {"revision": "prompt-v5", "same_stage_limit": 40,
               "no_ledger_reset": True, "original_freeze_sha256": digest(original)})
    budget = EvaluationBudget(tmp_path / "request-ledger.db", limit=40, fingerprint=digest(original))
    n = budget.reserve(role="generation", context="failed-original", content=b"test")
    budget.settle(n, status="failed", elapsed_ms=1, http_status=500)
    current = {**original, "code_fingerprint": "repaired"}
    if drift:
        current[drift] = "changed"
        with pytest.raises(EvaluationStopped): live.freeze_run(tmp_path, current, "prompt-v5")
    else:
        stage, fingerprint = live.freeze_run(tmp_path, current, "prompt-v5")
        assert stage == tmp_path / "revisions" / "prompt-v5"
        assert fingerprint == digest(original)
        assert EvaluationBudget(tmp_path / "request-ledger.db", limit=40, fingerprint=fingerprint).report()["used"] == 1
    assert live.read(tmp_path / "runtime-freeze.json") == original
    assert live.read(tmp_path / "state.json") == stopped


@pytest.mark.parametrize("tamper", [None, "request", "snapshot", "answer"])
def test_success_reuse_revalidates_same_input_and_raw_without_new_calls(tmp_path, tamper):
    from app.rag_context import build_evidence
    from app.quote_spans import task_evidence_payload
    from tests.test_question_jobs import question_fixture, submit, LocalRuntime, REPLY, QUESTION
    from app.question_worker import QuestionWorker
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    worker = QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime())
    assert worker.run_job(job["id"])
    job = env.service.get_job(job["id"])
    result = env.service.get_result(job["result_id"])
    snapshot = json.loads(env.service.store.row(job["id"])["snapshot_json"])
    retrieval = env.service.store.checkpoint(job["id"], "retrieval")
    pack = build_evidence(retrieval, env.settings, expected_document_id="qdoc",
                          payload_transform=task_evidence_payload).pack
    case = {"case_id": "test", "question": QUESTION}
    record = {"case_sha256": digest(case), "job": job, "result": result,
              "selected_chunk_ids": [i.chunk_id for i in pack.items]}
    request = {"system": env.model.calls[0][0], "user": json.loads(env.model.calls[0][1])}
    prior_snapshot = copy.deepcopy(snapshot)
    if tamper == "request": request["user"]["question"] += "changed"
    if tamper == "snapshot": prior_snapshot["index_id"] = "other"
    if tamper == "answer": record["result"]["answer"]["conclusion"][0]["text"] = "changed"
    counts = len(env.embedding.calls), len(env.model.calls)
    if tamper:
        with pytest.raises(EvaluationStopped):
            live.revalidate_saved_answer(record, request, REPLY, pack, case=case,
                                         snapshot=snapshot, prior_snapshot=prior_snapshot)
    else:
        live.revalidate_saved_answer(record, request, REPLY, pack, case=case,
                                     snapshot=snapshot, prior_snapshot=prior_snapshot)
    assert counts == (len(env.embedding.calls), len(env.model.calls))


@pytest.mark.parametrize("change_scope",[False,True])
def test_v6_targeted_repair_cannot_reset_36_calls_or_expand_four_questions(tmp_path,change_scope,monkeypatch):
    # 历史v6修订范围仍锁在v6，不让当前v7源码取得旧阶段的续费入口。
    monkeypatch.setattr(live,'TASK_PROMPT_VERSION','rag-task-v6')
    original={"approval_sha256":"a","plan_sha256":"p","cases_sha256":"c",
              "config":{"model":"fixed"},"sources":{"doc":"fixed"},"code_fingerprint":"old"}
    live.save(tmp_path/"runtime-freeze.json",original)
    live.save(tmp_path/"state.json",{"stopped":{"reason":"preserved initial failure"}})
    parent=tmp_path/"revisions"/"source-title-spacing";parent.mkdir(parents=True)
    live.save(parent/"runtime-freeze.json",{"code_fingerprint":"parent"})
    semantic={"overall":"not_fully_accepted"};live.save(tmp_path/"semantic-review-final.json",semantic)
    targets=["xlsx-revenue:Q2","xlsx-revenue:Q4","docx-learning:Q4","paper:Q3"]
    repair={"revision":"fact-contract-v6","same_stage_limit":40,"no_ledger_reset":True,
        "original_freeze_sha256":digest(original),"parent_stage":"revisions/source-title-spacing",
        "parent_freeze_sha256":digest(live.read(parent/"runtime-freeze.json")),
        "parent_semantic_sha256":digest(semantic),"targets":targets,"branch":"current","maximum_new_generations":4}
    if change_scope:repair["targets"]=["different"]
    live.save(tmp_path/"repair-fact-contract-v6.json",repair)
    ledger=EvaluationBudget(tmp_path/"request-ledger.db",limit=40,fingerprint=digest(original))
    for _ in range(36):
        n=ledger.reserve(role="generation",context="historical-synthetic",content=b"synthetic")
        ledger.settle(n,status="succeeded",elapsed_ms=1)
    if change_scope:
        with pytest.raises(EvaluationStopped):live.freeze_run(tmp_path,{**original,"code_fingerprint":"v6"},"fact-contract-v6")
        assert ledger.report()["used"]==36
    else:
        _,fingerprint=live.freeze_run(tmp_path,{**original,"code_fingerprint":"v6"},"fact-contract-v6")
        assert fingerprint==digest(original)
        restored=EvaluationBudget(tmp_path/"request-ledger.db",limit=40,fingerprint=fingerprint)
        for _ in range(4):
            n=restored.reserve(role="generation",context="targeted-synthetic",content=b"synthetic")
            restored.settle(n,status="succeeded",elapsed_ms=1)
        with pytest.raises(EvaluationStopped):restored.reserve(role="query",context="over-limit",content=b"synthetic")
        assert restored.report()["used"]==40


def test_current_prompt_cannot_reopen_historical_v6_repair_authorization(tmp_path):
    assert TASK_PROMPT_VERSION=='rag-task-v18'
    with pytest.raises(EvaluationStopped,match='修订'):
        live.freeze_run(tmp_path,{},'fact-contract-v6')
    assert not (tmp_path/'request-ledger.db').exists()
