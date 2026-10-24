"""逐事实原文范围、稳定身份和恢复的反例；只用合成文本及真实生产链路。"""
import copy
import json

import pytest

from app.rag_answer_contract import ANSWER_CONTRACT_VERSION, validate_task_output
from app.rag_citation_identity import citation_identity
from app.rag_context import EvidenceItem, EvidencePack
from app.rag_validation import ModelOutputError, validate_user_condition_boundary


def pack(text, ref=1, chunk="c1"):
    item = EvidenceItem(reference_id=ref, chunk_id=chunk, document_id="d", parse_version_id="v", page=1, text=text)
    return EvidencePack(items=[item], candidates=[item], index_id="i", parse_version_id="v", truncated=False, context_chars=0)


def fact(text="原文介绍一项规则。", ref=1, span=1, first=1, last=1, kind="direct"):
    return dict(text=text, refs=[ref], context=[], claim_type=kind,
                evidence=[dict(ref=ref, span=span, start_segment=first, end_segment=last)])


def body(facts=None, windows=None):
    facts = facts if facts is not None else [fact()]
    return dict(status="answered", conclusion=facts, explanation=[], clarification_questions=[], missing_points=[],
                evidence_quotes=windows or [dict(ref=1, span=1)],
                answer_parts=[dict(part_id="p1", state="answered", fact_indices=list(range(len(facts))), missing_detail="")])


def validate(value, source):
    return validate_task_output(json.dumps(value, ensure_ascii=False), source, question="请说明规则。",
                                contract_version='fact-context-parts-v4')


def test_each_fact_has_own_contiguous_range_even_in_same_chunk():
    source = pack("甲" * 60 + "乙" * 60 + "丙" * 60)
    output = validate(body([fact("第一项说明。"), fact("第三项说明。", first=3, last=3)]), source)
    assert output.quotes == {1: "甲" * 60, 2: "丙" * 60}
    assert [f.refs for f in output.conclusion] == [[1], [2]]
    assert output.citation_bindings == {1: dict(source_ref=1, char_start=0, char_end=60),
                                         2: dict(source_ref=1, char_start=120, char_end=180)}


def test_two_windows_of_one_input_chunk_can_support_different_claims():
    source = pack("甲" * 600 + "乙" * 60)
    output = validate(body([fact("开头说明。"), fact("末尾说明。", span=2, first=10, last=10)],
                           [dict(ref=1, span=1), dict(ref=1, span=2)]), source)
    assert output.quotes == {1: "甲" * 60, 2: "乙" * 60}
    assert output.citation_bindings[2]["char_start"] == 600


def test_repeated_quote_gets_distinct_occurrence_identity_and_crlf_basis():
    source = pack("😀" + "甲" * 59 + "乙" * 60 + "😀" + "甲" * 59)
    output = validate(body([fact(), fact(first=3, last=3)]), source)
    first = citation_identity("d", "v", "c1", 0, 60, output.quotes[1])
    third = citation_identity("d", "v", "c1", 120, 180, output.quotes[2])
    assert first["quote_sha256"] == third["quote_sha256"]
    assert first["evidence_id"] != third["evidence_id"]
    assert first["semantic_status"] == "not_reviewed"
    normalized = validate(body(), pack("第一行\r\n第二行。"))
    assert normalized.quotes[1] == "第一行\n第二行。"
    assert normalized.citation_bindings[1]["char_end"] == len("第一行\n第二行。")


def test_identity_survives_input_reference_reordering_but_changes_with_version():
    identities = []
    for ref in (1, 9):
        source = pack("唯一原文。", ref=ref)
        output = validate(body([fact(ref=ref)], [dict(ref=ref, span=1)]), source)
        binding = output.citation_bindings[1]
        identities.append(citation_identity("d", "v", "c1", binding["char_start"], binding["char_end"], output.quotes[1]))
    assert identities[0] == identities[1]
    assert citation_identity("d", "v2", "c1", 0, 5, "唯一原文。")["evidence_id"] != identities[0]["evidence_id"]


@pytest.mark.parametrize("change", ["missing_type", "unknown_type", "missing_evidence", "bool_segment", "float_ref",
    "outside_segment", "reversed_range", "undeclared_window", "duplicate_support", "unused_window", "invented_id"])
def test_bad_claim_selector_never_downgrades_to_legacy(change):
    value = body()
    claim = value["conclusion"][0]
    support = claim["evidence"][0]
    if change == "missing_type": claim.pop("claim_type")
    if change == "unknown_type": claim["claim_type"] = "certain"
    if change == "missing_evidence": claim.pop("evidence")
    if change == "bool_segment": support["start_segment"] = True
    if change == "float_ref": support["ref"] = 1.0
    if change == "outside_segment": support["end_segment"] = 99
    if change == "reversed_range": support.update(start_segment=2, end_segment=1)
    if change == "undeclared_window": support["span"] = 2
    if change == "duplicate_support": claim["evidence"].append(dict(support))
    if change == "unused_window": value["evidence_quotes"].append(dict(ref=1, span=2))
    if change == "invented_id": claim["evidence_id"] = "forged"
    with pytest.raises(ModelOutputError):
        validate(value, pack("甲" * 660))


def test_numeric_fact_and_context_cannot_borrow_other_range_of_same_window():
    source = pack("甲" * 60 + "本期收入21万元。")
    value = body([fact("收入为21万元。")])
    value["conclusion"][0]["context"] = [dict(kind="period", ref=1, span=1, segment=2)]
    with pytest.raises(ModelOutputError, match="context_not_in_claim_evidence"):
        validate(value, source)
    value["conclusion"][0]["context"][0]["segment"] = 1
    with pytest.raises(ModelOutputError, match="number_not_in_quote"):
        validate(value, source)


def test_user_phrase_boundary_reads_selected_quote_not_coincidentally_matching_input_ref():
    source = pack("甲" * 60 + "用户说的是经历，原文只记录规章。")
    value = body([fact("“用户自述的经历”无法判断。", first=2, last=2)])
    output = validate(value, source)
    with pytest.raises(ModelOutputError, match="mixed_user_source"):
        validate_user_condition_boundary(output, source, question="用户自述的经历应该如何判断？")


@pytest.mark.parametrize("kind", ["direct", "synthesis", "inference"])
def test_claim_type_is_a_declaration_not_a_semantic_pass(kind):
    output = validate(body([fact("所有分类必然一一对应。", kind=kind)]), pack("分类甲讨论目标；分类乙讨论手段。"))
    assert output.conclusion[0].claim_type == kind
    assert output.quotes[1] == "分类甲讨论目标；分类乙讨论手段。"
    # 故意不支持的结论仍可能形式合法：必须留给语义rubric，不能用标签冒充正确。


def test_real_job_keeps_id_type_range_and_exports_after_restart(tmp_path):
    from app.question_jobs import QuestionJobService
    from app.question_worker import QuestionWorker
    from app.repository import Repository
    from tests.test_question_jobs import LocalRuntime, question_fixture, submit
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    worker = QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime())
    assert worker.run_job(job["id"])
    completed = env.service.get_job(job["id"])
    result = env.service.get_result(completed["result_id"])
    citation = result["answer"]["citations"][0]
    claim = result["answer"]["conclusion"][0]
    assert claim["evidence_ids"] == [citation["evidence_id"]]
    assert claim["claim_type"] == "direct" and citation["verification"] == "exact_match"
    counts = len(env.model.calls), len(env.embedding.calls)
    restored = QuestionJobService(env.settings, Repository(tmp_path / "docqa.db"), env.rag)
    assert restored.get_result(result["id"])["answer"] == result["answer"]
    markdown = restored.export(result["id"], "markdown")[0]
    assert citation["evidence_id"] in markdown and "原文直述" in markdown and "语义支持未自动认证" in markdown
    assert counts == (len(env.model.calls), len(env.embedding.calls))


def test_real_job_publishes_two_ranges_from_one_input_reference(tmp_path):
    from app.question_worker import QuestionWorker
    from app.schemas import Chunk, SourceLocation
    from tests.test_question_jobs import LocalRuntime, question_fixture, submit
    env = question_fixture(tmp_path)
    # 新生产策略要求完整句，旧无句末范围仍在上方裸v4兼容用例中验证。
    text = ("甲" * 59 + "。") * 10 + "乙" * 59 + "。"
    env.repository.finish_parse("qdoc", 1, [Chunk(id="two-window", document_id="qdoc", page=1, text=text,
        parse_version_id="two-window-v", order_index=0, chunk_type="text", sources=[SourceLocation(format="pdf", page=1)])])
    env.rag.index.build("qdoc", rebuild=True)
    env.embedding.calls.clear()
    def unit_reply(_system,user):
        from tests.fixtures.rag_quality.claim_protocol import declare_parts
        value=body([fact('开头说明。'),fact('末尾说明。')])
        value.pop('evidence_quotes')
        # 选择当前公布的首、末句单元；旧大单元编号不应误选为两次相同引文。
        units=sorted(map(int,json.loads(user)['references'][0]['quote_units']))
        for f,unit in zip(value['conclusion'],[units[0],units[-1]]):f['evidence']=[dict(ref=1,unit=unit)]
        return json.dumps(declare_parts(value,derive_refs=True),ensure_ascii=False)
    env.model.handler=unit_reply
    job, _ = submit(env, question="请说明规则。")
    assert QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job["id"])
    completed = env.service.get_job(job["id"])
    assert completed["status"] == "succeeded", completed
    answer = env.service.get_result(completed["result_id"])["answer"]
    assert [c["source_reference_id"] for c in answer["citations"]] == [1, 1]
    assert [c["char_start"] for c in answer["citations"]] == [0, 600]
    assert [c["quote"] for c in answer["citations"]] == [text[:60], text[600:]]
    assert len(env.embedding.calls) == len(env.model.calls) == 1


@pytest.mark.parametrize("field,value", [("evidence_id", "ev-forged"), ("char_start", 1),
    ("source_reference_id", 99), ("quote_sha256", "fake"), ("offset_basis", None)])
def test_restored_answer_rechecks_identity_without_calls(tmp_path, field, value):
    from app.question_worker import QuestionWorker
    from tests.test_question_jobs import LocalRuntime, question_fixture, submit
    env = question_fixture(tmp_path)
    worker = QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime())
    first, _ = submit(env)
    assert worker.run_job(first["id"])
    result = env.service.get_result(env.service.get_job(first["id"])["result_id"])
    later, _ = submit(env, key="tampered-identity")
    claimed = worker.store.claim(worker.worker_id, 60, job_id=later["id"])
    answer = copy.deepcopy(result["answer"])
    answer["citations"][0][field] = value
    for role, saved in (("evidence", worker.store.checkpoint(first["id"], "evidence")), ("answer", answer)):
        assert worker.store.save_checkpoint(later["id"], claimed["lease_token"], role=role, payload=saved, sequence_no=None)
    counts = len(env.model.calls), len(env.embedding.calls)
    worker.process_job(claimed)
    completed = env.service.get_job(later["id"])
    assert completed["status"] == "failed" and not completed["result_id"]
    assert counts == (len(env.model.calls), len(env.embedding.calls))
    assert env.service.get_result(result["id"])["answer"] == result["answer"]
