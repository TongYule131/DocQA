"""独立预算/槽位的生产路径离线验证，不调用真实模型。"""
from dataclasses import replace

import pytest

from app.config import Settings
from app.rag import RagService
from app.rag_context import build_evidence
from app.rag_prompts import build_task_system_prompt, build_system_prompt
from app.quote_spans import task_segment_payload
from app.retrieval_planning import plan_retrieval
from app.rag_evidence_slots import evidence_slots
from tests.test_retrieval_planning import chunk, vector_result
from tests.test_question_jobs import question_fixture, submit, LocalRuntime
from app.question_worker import QuestionWorker


def test_question_budget_env_is_independent_and_environment_can_override(tmp_path, monkeypatch):
    path=tmp_path/'config.env'
    path.write_text('DOCQA_QUESTION_CONTEXT_K=48\nDOCQA_QUESTION_CANDIDATE_K=80\n'
        'DOCQA_QUESTION_CONTEXT_MAX_CHARS=40000\nDOCQA_QUESTION_INPUT_MAX_CHARS=48000\n',encoding='utf-8')
    settings=Settings.from_env(path)
    assert (settings.question_context_k,settings.question_candidate_k)==(48,80)
    assert (settings.question_context_max_chars,settings.question_input_max_chars)==(40000,48000)
    assert (settings.rag_context_max_chars,settings.rag_input_max_chars)==(12000,20000)
    assert settings.analysis_input_max_chars==20000
    monkeypatch.setenv('DOCQA_QUESTION_CONTEXT_MAX_CHARS','36000')
    assert Settings.from_env(path).question_context_max_chars==36000


@pytest.mark.parametrize('options',[
    {'question_context_k':0},{'question_context_k':65},{'question_candidate_k':101},
    {'question_candidate_k':True},{'question_input_max_chars':0},{'question_input_max_chars':True},
    {'question_context_max_chars':32000},{'question_context_max_chars':0}])
def test_question_caps_reject_inconsistent_or_unbounded_count(options):
    with pytest.raises(ValueError): Settings(**options)


def test_larger_task_cap_keeps_legacy_overhead_guard():
    settings=Settings(rag_context_max_chars=10,rag_input_max_chars=100)
    service=RagService(settings,None,None,None)
    assert service._context_cap(build_task_system_prompt(), '问题', '文件')>12000
    from app.rag_context import RagError
    with pytest.raises(RagError):
        service._context_cap(build_system_prompt(),'问题','文件',task_protocol=False)


def test_context_above_twenty_is_allowed_without_changing_vector_top_k():
    settings=Settings()
    chunks=[chunk(i,f'设备{i}的规则已记录，具体条件必须核对原文。') for i in range(40)]
    planned=vector_result(chunks)
    built=build_evidence(planned,settings,expected_document_id='doc',max_items=settings.question_context_k,
        context_max_chars=settings.question_context_max_chars,payload_transform=task_segment_payload)
    assert len(built.pack.items)==32
    assert settings.rag_retrieval_k==8 and settings.rag_context_k==5
    assert built.pack.context_chars<=24000


def test_named_cases_and_explicit_subsections_reserve_different_evidence():
    chunks=[chunk(0,'旧材料讨论系统背景，包含大量通用词语。','背景'),
        chunk(1,'《甲工程》遇到接口适配困难。','2 机制 / 2.1 接口'),
        chunk(2,'接口机制要求核对设备双方协议。','2 机制 / 2.1 接口'),
        chunk(3,'《乙工程》采用统一调度并保留当地规则。','2 机制 / 2.2 调度'),
        chunk(4,'连接平台是指提供传输通道的基础设施。','2 机制 / 2.3 平台'),
        chunk(5,'结论是适配与调度需配合，同时保留差异。','3 结论')]
    question='对比《甲工程》与《乙工程》，并用第2节的机制解释不同表现。'
    planned=plan_retrieval(question,vector_result(chunks[:1]),chunks,max_candidates=8)
    slots=planned['planning']['evidence_slots']
    assert {'named_case','explicit_subsection','author_conclusion'} <= {s['kind'] for s in slots}
    built=build_evidence(planned,Settings(rag_context_k=5),expected_document_id='doc')
    assert {'c1','c3','c4','c5'} <= {i.chunk_id for i in built.pack.items}
    assert built.pack.slot_hints['unfilled_count']==0
    assert not built.pack.slot_hints['semantic_completeness_verified']
    assert all(i.text==chunks[int(i.chunk_id[1:])].text for i in built.pack.items)


def test_budget_cannot_fit_a_slot_is_reported_without_claiming_full_coverage():
    chunks=[chunk(0,'《甲工程》的条件。','2.1 接口'),chunk(1,'《乙工程》的条件。','2.2 调度')]
    planned=plan_retrieval('比较《甲工程》与《乙工程》，解释第2节机制。',vector_result(chunks),chunks)
    built=build_evidence(planned,Settings(rag_context_k=1),expected_document_id='doc')
    assert built.pack.slot_hints['unfilled_count']>0
    assert any('槽位未装入' in text for text in built.limitations)


def test_changing_task_budget_after_submit_fails_before_any_provider_call(tmp_path):
    env=question_fixture(tmp_path)
    job,_=submit(env)
    env.rag.settings=replace(env.settings,question_context_max_chars=25000)
    worker=QuestionWorker(env.rag.settings,env.repository,rag=env.rag,runtime=LocalRuntime())
    worker.run_job(job['id'])
    current=env.service.get_job(job['id'])
    assert current['status']=='failed' and current['error_code']=='question_snapshot_changed'
    assert not env.embedding.calls and not env.model.calls


def test_slot_plan_never_uses_hardcoded_page_or_document_names():
    pool=vector_result([chunk(7,'普通正文。','8 普通说明')])['results']
    assert evidence_slots('普通事实是什么？',pool)==[]
