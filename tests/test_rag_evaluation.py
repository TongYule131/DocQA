"""度量与关卡的反例：合法引用、命中数字和部分证据均不能冒充语义通过。"""
import copy
import json
import runpy
from pathlib import Path

import pytest

from app.rag_evaluation import (EvaluationError,digest,evidence_metrics,
                                reviewed_answer_metrics,retrieval_gate)
from app.rag_context import EvidenceItem,EvidencePack
from app.rag_validation import validate_model_output
from scripts.evaluate_rag_quality import evaluate,main

GOLD=Path(__file__).parent/'fixtures/rag_quality/golden.py'


@pytest.fixture
def case():
    return runpy.run_path(str(GOLD))["make_cases"]()[0]


def test_joint_evidence_requires_whole_group_and_counts_actual_input(case):
    ids=[c["id"] for c in case["chunks"]]
    metrics=evidence_metrics(case,ids,[ids[3],ids[4],ids[5]])
    assert metrics["candidate_recall"]["value"]==1
    assert metrics["input_recall"]["numerator"]==2
    assert metrics["input_recall"]["missing_points"]==["p3"]


def test_alternative_evidence_is_not_counted_twice(case):
    point=case["required_evidence"][0]
    point["alternatives"].append(point["alternatives"][0])
    ids=[c["id"] for c in case["chunks"]]
    assert evidence_metrics(case,ids,ids)["input_recall"]["numerator"]==3


def test_compression_cannot_count_gold_quote_in_an_unread_range(case):
    from app.rag_evaluation import admitted_quote_metrics
    fragment=case['required_evidence'][0]['alternatives'][0][0]
    source=next(c for c in case['chunks'] if c['id']==fragment['chunk_id'])
    full={c['id']:[[0,len(c['text'])]] for c in case['chunks']}
    assert admitted_quote_metrics(case,full)['numerator']==len(case['required_evidence'])
    full[source['id']]=[[0,1]]
    assert case['required_evidence'][0]['point_id'] in admitted_quote_metrics(case,full)['missing_points']


def test_unlabelled_relevance_is_unknown_not_zero(case):
    case["relevance_exhaustive"]=False
    ids=[c["id"] for c in case["chunks"]]
    assert evidence_metrics(case,ids,ids)["input_precision"]["value"] is None


def test_no_answer_case_is_not_given_perfect_recall_or_semantic_pass():
    case=runpy.run_path(str(GOLD))["make_cases"]()[-1]
    ids=[c["id"] for c in case["chunks"]]
    metrics=evidence_metrics(case,ids,ids)
    assert metrics["input_recall"]["value"] is None
    assert metrics["generated_answer_quality"]=="not_evaluated_by_retrieval_metrics"
    assert reviewed_answer_metrics(case,None,None)["faithfulness"] is None


@pytest.mark.parametrize("mutation",["quote","version","unknown_chunk","duplicate_selected"])
def test_corrupt_gold_and_mixed_scope_never_count_as_pass(case,mutation):
    ids=[c["id"] for c in case["chunks"]]
    if mutation=="quote":case["required_evidence"][0]["alternatives"][0][0]["quote"]="伪造原文"
    if mutation=="version":case["chunks"][0]["parse_version_id"]="another-version"
    if mutation=="unknown_chunk":ids.append("another-document:c1")
    selected=ids+ids[:1] if mutation=="duplicate_selected" else ids
    with pytest.raises(EvaluationError):evidence_metrics(case,ids,selected)


def review(case,answer):
    return dict(case_id=case["case_id"],case_sha256=digest(case),answer_sha256=digest(answer),
        reviewer="fixture-manual-review",independent=False,
        fact_checks=[dict(index=0,supported=False,relevant=True,citation_supported=False,conditions_complete=False)],
        covered_points=[],status_appropriate=False,severe_errors=["错误数值或决定性条件丢失"])


def test_wrong_fact_with_real_quote_does_not_pass_semantic_review(case):
    # 引文真实仍不足：复核绑定这份错误答案，不能只看数字命中或编号存在。
    source=case["chunks"][3]
    pack=EvidencePack(items=[EvidenceItem(reference_id=1,chunk_id=source["id"],
        document_id=source["document_id"],parse_version_id=source["parse_version_id"],
        page=None,text=source["text"])],candidates=[],index_id="test-index",
        parse_version_id=source["parse_version_id"],truncated=False,context_chars=0)
    answer={"conclusion":[{"text":"模型只有四个阶段且必然有效。","refs":[1]}],"explanation":[]}
    validated=validate_model_output(json.dumps({**answer,"status":"answered",
        "clarification_questions":[],"evidence_quotes":[{"ref":1,"span":1}]},ensure_ascii=False),
        pack,task_protocol=True)
    assert validated.quotes[1]==source["text"]  # 真实生产形式校验通过，语义仍必须失败。
    metrics=reviewed_answer_metrics(case,answer,review(case,answer))
    assert metrics["faithfulness"]["value"]==0
    assert metrics["citation_support"]["value"]==0
    assert not metrics["status_appropriate"] and metrics["severe_errors"]


@pytest.mark.parametrize("mutation",["answer","source","count","bool","reviewer"])
def test_review_is_bound_to_exact_answer_and_source_and_every_fact(case,mutation):
    answer={"conclusion":[{"text":"原文内容","refs":[1]}],"explanation":[]}
    checked=review(case,answer)
    if mutation=="answer":answer["conclusion"][0]["text"]="不同答案"
    if mutation=="source":case["chunks"][0]["text"]+="新版本"
    if mutation=="count":checked["fact_checks"]=[]
    if mutation=="bool":checked["fact_checks"][0]["supported"]=1
    if mutation=="reviewer":checked["reviewer"]=""
    with pytest.raises(EvaluationError):reviewed_answer_metrics(case,answer,checked)


def test_gate_rejects_regression_even_if_overall_can_look_good(case):
    report=evaluate([case])
    baseline=copy.deepcopy(report)
    report["cases"][0]["retrieval"]["input_recall"]["value"]=0.5
    gate=retrieval_gate(report,baseline)
    assert not gate["passed"] and gate["failures"][0]["reason"]=="regression"
    assert gate["online_semantic_release"]=="unverified"


def test_gate_rejects_comparing_changed_labels(case):
    baseline=evaluate([case])
    case["question"]+="不同问题"
    with pytest.raises(EvaluationError,match="baseline_case_mismatch"):
        retrieval_gate(evaluate([case]),baseline)


def test_default_golden_gate_no_provider_and_report_is_not_overwritten(tmp_path,capsys):
    target=tmp_path/'report.json'
    assert main(["--report",str(target),"--gate"])==0
    report=json.loads(target.read_text(encoding="utf-8"))
    assert report["summary"]["overall"]["case_count"]==14
    assert report["summary"]["overall"]["provider_calls_this_run"]==0
    assert report["summary"]["overall"]["generated_semantic_passes"] is None
    assert report["gate"]["scope"]=="offline_retrieval_only"
    with pytest.raises(SystemExit):main(["--report",str(target)])


def test_actual_pack_budget_and_original_text_are_preserved_for_all_golden_cases():
    cases=runpy.run_path(str(GOLD))["make_cases"]()
    report=evaluate(cases)
    for case,row in zip(cases,report["cases"]):
        assert row["context_chars"]<=case["context_max_chars"]
        assert len(row["selected_ids"])<=case["context_k"]
        assert row["retrieval"]["input_recall"]["value"] in (None,1)


def test_chunk_id_alone_does_not_prove_actual_input_contains_evidence(case):
    case["vector_result"]["results"][3]["text"]="同一编号被替换成不含必要事实的文字。"
    with pytest.raises(EvaluationError,match="run_text_or_scope_changed"):
        evaluate([case])
