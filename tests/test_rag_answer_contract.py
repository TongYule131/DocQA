import copy
import json

import pytest

from app.rag_answer_contract import question_parts,validate_task_output,fact_display_text
from app.rag_validation import ModelOutputError,render_markdown
from scripts.evaluate_answer_contract import pack_for,evaluate
from tests.fixtures.rag_quality.answer_contract import make_cases,semantic_probes


@pytest.mark.parametrize("case",make_cases(),ids=lambda c:c["case_id"])
def test_frozen_positive_and_negative_pass_through_production_contract(case):
    pack=pack_for(case)
    raw=json.dumps(case["positive"],ensure_ascii=False)
    output=validate_task_output(raw,pack,question=case["question"])
    assert output.conclusion[0].text==case["positive"]["conclusion"][0]["text"]
    assert output.quotes[1]==case["source"]
    assert all(value in render_markdown(output) for _,value in case["required_context"])
    with pytest.raises(ModelOutputError):
        validate_task_output(json.dumps(case["negative"],ensure_ascii=False),pack,question=case["question"])


@pytest.mark.parametrize("case",semantic_probes(),ids=lambda c:c["case_id"])
def test_structural_acceptance_does_not_certify_all_original_conditions(case):
    # 缺少真正限定的负例仍可结构合法；不得用这个检查器虚构语义全通过。
    output=validate_task_output(json.dumps(case["negative"],ensure_ascii=False),pack_for(case),question=case["question"])
    omitted=case["required_context"][-1][1]
    assert omitted not in fact_display_text(output.conclusion[0])
    assert case["requires_semantic_review"]


def test_question_parts_are_original_slices_and_do_not_drop_long_tail():
    question="收入多少？成本多少？利润缺哪些依据？"
    assert [p["text"] for p in question_parts(question)]==["收入多少？","成本多少？","利润缺哪些依据？"]
    long="；".join(f"问题{i}" for i in range(12))
    assert len(question_parts(long))==8
    assert "".join(p["text"] for p in question_parts(long))==long


@pytest.mark.parametrize("change",["empty_numeric_context","wrong_ref","bool_index","unknown_part","unknown_context_key"])
def test_context_and_coverage_reject_untrusted_scope_or_fields(change):
    case=make_cases()[0];body=copy.deepcopy(case["positive"])
    if change=="empty_numeric_context":body["conclusion"][0]["context"]=[]
    if change=="wrong_ref":body["conclusion"][0]["context"][0]["ref"]=2
    if change=="bool_index":body["answer_parts"][0]["fact_indices"]=[True]
    if change=="unknown_part":body["answer_parts"][0]["part_id"]="invented"
    if change=="unknown_context_key":body["conclusion"][0]["context"][0]["confidence"]=1
    with pytest.raises(ModelOutputError):validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question=case["question"])


def test_missing_part_status_is_not_silently_upgraded_and_comparison_does_not_require_definition():
    case=make_cases()[0];body=copy.deepcopy(case["positive"])
    body.update(status="partial",missing_points=["缺少补助依据。"])
    body["answer_parts"].append({"part_id":"p2","state":"missing_evidence","fact_indices":[],"missing_detail":"缺少补助依据。"})
    output=validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question=case["question"]+"补助多少？")
    assert output.status=="partial"
    body["answer_parts"][1]["missing_detail"]="未提供过程参数的正式定义。"
    body["missing_points"]=[body["answer_parts"][1]["missing_detail"]]
    with pytest.raises(ModelOutputError,match="unsupported_requirement"):
        validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question=case["question"]+"预算与实际有何区别？")


def test_offline_report_does_not_promote_semantic_omission_probes_to_accuracy():
    report=evaluate()
    assert report["guard_passed"] and report["provider_calls"]==0
    assert report["semantic_accuracy"] is None
    assert len(report["semantic_probes"])==8


def _context_environment(tmp_path):
    from tests.test_question_jobs import question_fixture
    from app.schemas import Chunk,SourceLocation
    env=question_fixture(tmp_path)
    case=make_cases()[0]
    env.repository.finish_parse("qdoc",1,[Chunk(id="context-c",document_id="qdoc",page=1,
        text=case["source"],parse_version_id="context-v",order_index=0,
        sources=[SourceLocation(format="txt",line_start=1,line_end=1)])])
    env.rag.index.build("qdoc",rebuild=True)
    env.embedding.calls.clear()
    env.model.handler=lambda *_:json.dumps(case["positive"],ensure_ascii=False)
    return env,case


def test_context_is_visible_in_same_fact_and_durable_across_history_and_exports(tmp_path):
    from tests.test_question_jobs import submit,LocalRuntime
    from app.question_worker import QuestionWorker
    from app.question_jobs import QuestionJobService
    from app.repository import Repository
    env,case=_context_environment(tmp_path)
    job,_=submit(env,question=case["question"])
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job["id"])
    completed=env.service.get_job(job["id"])
    assert completed["status"]=="succeeded",completed
    result=env.service.get_result(completed["result_id"])
    answer=result["answer"]
    assert "未经审计" not in answer["conclusion"][0]["text"]  # 正文没有被自动改写。
    assert answer["conclusion"][0]["context"][-1]["text"]=="未经审计"
    assert "未经审计" in answer["answer"] and "2025年" in answer["answer"]
    restored=QuestionJobService(env.settings,Repository(tmp_path/"docqa.db"),env.rag)
    assert restored.get_result(result["id"])["answer"]==answer
    assert "未经审计" in restored.export(result["id"],"markdown")[0]
    assert "answer_parts" in restored.export(result["id"],"json")[0]
    assert len(env.model.calls)==len(env.embedding.calls)==1


def test_restored_checkpoint_cannot_replace_valid_condition_with_another_claim(tmp_path):
    from tests.test_question_jobs import submit,LocalRuntime
    from app.question_worker import QuestionWorker
    env,case=_context_environment(tmp_path)
    worker=QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime())
    first,_=submit(env,question=case["question"])
    assert worker.run_job(first["id"])
    original=env.service.get_result(env.service.get_job(first["id"])["result_id"])
    next_job,_=submit(env,key="restore-condition",question=case["question"])
    claimed=worker.store.claim(worker.worker_id,60,job_id=next_job["id"])
    evidence=worker.store.checkpoint(first["id"],"evidence")
    answer=copy.deepcopy(original["answer"])
    answer["conclusion"][0]["context"][-1]["text"]="已经审计"
    for role,value in [("evidence",evidence),("answer",answer)]:
        assert worker.store.save_checkpoint(next_job["id"],claimed["lease_token"],role=role,payload=value,sequence_no=None)
    counts=len(env.model.calls),len(env.embedding.calls)
    worker.process_job(claimed)
    failed=env.service.get_job(next_job["id"])
    assert failed["status"]=="failed" and not failed["result_id"]
    assert counts==(len(env.model.calls),len(env.embedding.calls))
    assert env.service.get_result(original["id"])["answer"]==original["answer"]
