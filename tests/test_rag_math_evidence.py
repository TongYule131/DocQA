"""S1/S2：合成公式、原文范围及真实任务路径；不读取私有文档或供应商。"""
from copy import deepcopy
import json

import pytest

from app.rag_math_evidence import formula_info,math_evidence,admission_partners,group_hints
from app.rag_numeric_roles import source_number_tokens,source_number_mentions,numeric_view
from app.rag_context import build_evidence,pack_evidence,EvidenceItem
from app.config import Settings
from app.retrieval_planning import plan_retrieval
from app.rag_evidence_slots import evidence_slots,slot_hints
from app.rag_slot_ranges import visible_ranges
from app.quote_spans import task_segment_payload
from tests.test_retrieval_planning import chunk,vector_result


@pytest.mark.parametrize('text,kind,state',[
    ('k^{CB}<k^{CS}',None,'readable_expression'),
    (r'\frac{\eta x}{2}', 'formula','readable_expression'),
    ('其中η代表公平关切系数。',None,'not_formula'),
    ('推论7解释：宣传水平高于另一情形。',None,'not_formula'),
    ('k_A < \ufffd','formula','unreadable_expression'),
    ('k_A <','formula','unreadable_expression'),
    ('k^{A < k_B','formula','unreadable_expression'),
    ('','formula','empty_formula'),
    ('<p>正文</p>',None,'not_formula'),
    ('年龄 < 18。',None,'readable_expression'),
    ('温度 = 37℃。',None,'readable_expression'),
    ('不等号“>”表示高于。',None,'not_formula'),
])
def test_math_representation_is_not_automatically_semantically_verified(text,kind,state):
    result=formula_info(text,kind)
    assert result['state']==state
    assert all(text[a:b] for a,b in result['ranges'])


def source_chunks():
    return [chunk(0,'推论7：k_1 < k_2。','3 宣传决策'),
        chunk(1,'推论7表明，在相同投入条件下，第一种情形的宣传水平高于第二种。','3 宣传决策'),
        chunk(2,'推论7中，η表示公平关切系数，比较限于同一市场。','3 宣传决策'),
        chunk(3,'推论8说明成本有所减少。','3 宣传决策')]


def test_explicit_same_statement_binds_formula_body_and_conditions_without_repairing_conflict():
    chunks=source_chunks();before=deepcopy([c.model_dump() for c in chunks])
    report=math_evidence(vector_result(chunks)['results'],'核对推论7的公式与正文是否矛盾')
    group=report['groups'][0]
    assert group['chunk_ids']==['c0','c1','c2'] and 'c3' not in group['chunk_ids']
    assert group['formula_ids']==['c0'] and group['explanation_ids']==['c1']
    assert group['definition_ids']==['c2'] and not group['semantic_support_verified']
    assert [c.model_dump() for c in chunks]==before
    assert not report['formula_semantics_verified']


@pytest.mark.parametrize('mutation',['other_document','other_version','other_section','other_statement','too_far'])
def test_nearby_text_alone_cannot_create_a_mathematical_support_group(mutation):
    pool=vector_result(source_chunks()[:2])['results']
    if mutation=='other_document':pool[1]['document_id']='other'
    if mutation=='other_version':pool[1]['parse_version_id']='other'
    if mutation=='other_section':pool[1]['heading_path']='4 其他问题'
    if mutation=='other_statement':pool[1]['text']=pool[1]['text'].replace('推论7','推论8')
    if mutation=='too_far':pool[1:1]=[dict(pool[1],chunk_id=f'gap-{i}',text='无关正文。') for i in range(5)]
    assert math_evidence(pool)['groups']==[]


def build(chunks,*,cap=4,chars=16000,settings=None):
    q='核对推论7的公式与正文是否一致，并保留符号定义与条件'
    planned=plan_retrieval(q,vector_result(chunks[:1]),chunks,max_candidates=16,max_items=cap,fulltext_budget=chars,
        payload_transform=task_segment_payload)
    result=build_evidence(planned,settings or Settings(),expected_document_id='doc',max_items=cap,
        context_max_chars=chars,payload_transform=task_segment_payload,extractive_question=q)
    return planned,result


def test_math_group_is_atomic_under_count_budget_and_keeps_independent_quote_units():
    _,enough=build(source_chunks(),cap=3)
    assert {i.chunk_id for i in enough.pack.items}=={'c0','c1','c2'}
    assert enough.pack.math_hints['incomplete_count']==0
    for item in enough.pack.items:
        assert all({p[0] for p in parts}=={item.reference_id} for parts in item.unit_choices.values()),'数学关联不能合成为跨片段quote'
    _,limited=build(source_chunks(),cap=2)
    assert not ({'c0','c1','c2'} & {i.chunk_id for i in limited.pack.items})
    assert limited.pack.math_hints['incomplete_count']==1


def test_math_group_does_not_survive_half_missing_at_candidate_or_score_filter():
    chunks=source_chunks()
    planned,_=build(chunks)
    planned['results']=[h for h in planned['results'] if h['chunk_id']!='c2']
    result=build_evidence(planned,Settings(),expected_document_id='doc',max_items=8,context_max_chars=16000,
        payload_transform=task_segment_payload,extractive_question='核对公式和正文')
    assert not ({'c0','c1'} & {i.chunk_id for i in result.pack.items})
    assert result.pack.math_hints['groups'][0]['state']=='candidate_unavailable'
    assert any('缺少成员' in reason for reason in result.pack.drop_reasons)


def test_bad_formula_is_not_admitted_but_unrelated_prose_still_works():
    chunks=[chunk(0,'k_1 < \ufffd','3 机制'),chunk(1,'正文说明，宣传应尊重当地规则。','3 机制')]
    _,result=build(chunks)
    assert 'c0' not in {i.chunk_id for i in result.pack.items}
    assert result.pack.math_hints['blocked_count']==1
    plain=plan_retrieval('宣传应遵循什么规则',vector_result(chunks[1:]),chunks)
    prose=build_evidence(plain,Settings(),expected_document_id='doc',extractive_question='宣传应遵循什么规则')
    assert any('尊重当地规则' in i.text for i in prose.pack.items)


def test_actual_admitted_ranges_not_chunk_presence_determine_slot_coverage():
    text='《甲工程》面临接口困难。'+'无关背景介绍。'*150+'后续另有其他策略。'
    pool=vector_result([chunk(0,text)])['results']
    slots=evidence_slots('比较《甲工程》与《乙工程》',pool)
    item=EvidenceItem(1,'c0','doc','v1',1,text,read_ranges=[[len(text)-10,len(text)]],segment_policy='sentence-segments-v2')
    hints=slot_hints(slots,{'c0'},visible_ranges([item]))
    assert hints['slots'][0]['state']=='outside_admitted_ranges'
    assert hints['slots'][0]['selected_candidate_count']==1 and hints['unfilled_count']>0


def test_ocr_search_projection_preserves_original_offsets():
    text='传 输网 格是 指连接节点的基础设施。'
    slots=evidence_slots('“传输网格”应如何定位',vector_result([chunk(0,text)])['results'])
    definition=next(s for s in slots if s['kind']=='direct_definition')
    assert any('是 指' in text[a:b] for a,b in definition['candidate_ranges']['c0'])


def test_formula_indices_and_references_cannot_back_business_values():
    source='第8页，参考文献[17]，k_{1}<k_2，x^3，2026年投入63.7万元。'
    assert source_number_tokens(source)=={'2026','63.7'}
    assert [(v['value'],v['kind']) for v in source_number_mentions(source)]==[('2026','time'),('63.7','value')]
    assert source_number_tokens('k_12的成本为12万元。')=={'12'}
    assert source_number_tokens('k_{-1}和x^−2。')==set()
    assert all(source[v['start']:v['end']] for v in source_number_mentions(source))


def test_supported_formula_indices_are_checked_against_variable_identity():
    from app.rag_validation import ModelOutputError
    item=EvidenceItem(1,'c','d','v',1,'k_{1}<k_2。')
    assert not source_number_tokens(numeric_view('k_1<k_2。',[item],[item.text]))
    with pytest.raises(ModelOutputError,match='locator_not_supported'):
        numeric_view('m_1<k_2。',[item],[item.text])


def test_anchor_sentence_and_its_adjacent_condition_are_forced_into_real_context():
    chunks=[chunk(0,'《甲工程》采用本地接口。仅在完成身份验证后适用。'+('背景说明涉及通用设备。'*120)+'下一阶段计划调整调度策略。','1 机制'),
        chunk(1,'《乙工程》采用云端调度。','2 机制')]
    q='比较《甲工程》和《乙工程》，重点解释下一阶段计划调整调度策略。'
    planned=plan_retrieval(q,vector_result(chunks),chunks)
    result=build_evidence(planned,Settings(),expected_document_id='doc',max_items=6,context_max_chars=16000,
        payload_transform=task_segment_payload,extractive_question=q)
    item=next(i for i in result.pack.items if i.chunk_id=='c0')
    actual=''.join(item.text[a:b] for a,b in visible_ranges([item])['c0'])
    assert '《甲工程》采用本地接口。' in actual
    assert '仅在完成身份验证后适用。' in actual
    assert not result.pack.slot_hints['unfilled_count']


def test_empty_formula_region_remains_visible_in_quality_diagnostics_without_fake_chunk():
    from app.schemas import Block,SourceLocation
    from app.rag_source_diagnostics import source_diagnostics,public_source_diagnostics
    block=Block(id='empty',document_id='doc',parse_version_id='v1',order_index=2,block_type='formula',text='',
        sources=[SourceLocation(format='pdf',page=3)])
    report=source_diagnostics([chunk(0,'正文仍可回答普通问题。')],None,blocks=[block])
    assert report['unindexed_formula_count']==1 and report['unindexed_formula_regions'][0]['formula']['state']=='empty_formula'
    assert public_source_diagnostics(report)['unindexed_formula_count']==1
    assert 'unindexed_formula_regions' not in public_source_diagnostics(report)


def test_only_explicit_following_symbol_definition_is_linked_without_statement_label():
    pool=vector_result([chunk(0,'推论7：k_1<k_2。','3 决策'),
        chunk(1,'其中，k表示宣传水平。','3 决策'),chunk(2,'推论7说明第一种水平高于第二种。','3 决策')])['results']
    assert math_evidence(pool)['groups'][0]['definition_ids']==['c1']
    pool[1]['text']='其中，η表示另一个研究中的系数。'
    assert math_evidence(pool)['groups'][0]['definition_ids']==[]


def library_task(tmp_path,*,cap=12,broken=False):
    from tests.test_question_jobs import question_fixture
    from app.product_access import ProductAccess
    from app.knowledge_questions import KnowledgeQuestions
    env=question_fixture(tmp_path,question_context_k=cap)
    chunks=source_chunks()[:3]
    if broken:chunks[0]=chunks[0].model_copy(update={'text':'推论7：k_1 < \ufffd'})
    chunks=[c.model_copy(update={'document_id':'qdoc','parse_version_id':'math-v1'}) for c in chunks]
    env.repository.finish_parse('qdoc',4,chunks)
    env.rag.index.build('qdoc')
    access=ProductAccess(env.repository,env.settings,env.rag.index)
    base=access.create_base('合成数学资料','')
    access.bind_document(base['id'],'qdoc');access.publish(base['id'],0)
    env.embedding.calls.clear();env.model.calls.clear()
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'c')
    task=service.submit(conv['id'],'MATH-PRIVATE-MARKER：核对推论7公式与正文解释是否一致','q')
    return env,service,task


def test_real_library_worker_informs_model_of_unverified_math_and_keeps_original_citations(tmp_path):
    from app.knowledge_worker import KnowledgeWorker
    from app.knowledge_traces import KnowledgeTraces
    from tests.test_question_jobs import LocalRuntime
    env,service,task=library_task(tmp_path)
    def respond(system,user):
        prompt=json.loads(user)
        assert prompt['mathematical_evidence']['representation_status']=='readable_text_unverified'
        assert prompt['mathematical_evidence']['semantic_verification']=='not_automatically_verified'
        formula=next(e for e in prompt['references'] if 'k_1 < k_2' in str(e['content']))
        body=next(e for e in prompt['references'] if '宣传水平高于第二种' in str(e['content']))
        return json.dumps(dict(status='answered',conclusion=[dict(text='公式为k_1<k_2，正文称第一种情形的宣传水平高于第二种，两端表述存在矛盾。',
            claim_type='inference',context=[],evidence=[dict(ref=formula['ref'],unit=1),dict(ref=body['ref'],unit=1)],part_ids=['p1'])],
            explanation=[],clarification_questions=[],missing_points=[],answer_parts=[dict(part_id='p1',state='answered',missing_detail='')]),ensure_ascii=False)
    env.model.handler=respond
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    row=service.present(service.require_task(task['id']))
    assert row['status']=='succeeded',row
    assert row['requests_used']==2 and len(env.model.calls)==1 and len(env.embedding.calls)==1
    assert len(row['result']['citations'])==2
    assert row['result']['retrieval']['math_evidence']['incomplete_count']==0
    trace=KnowledgeTraces(env.repository).detail(task['id'])
    diagnostic=next(d for d in trace['diagnostics'] if d['kind']=='retrieval')
    assert diagnostic['math_evidence']['total']==1
    assert 'MATH-PRIVATE-MARKER' not in json.dumps(trace,ensure_ascii=False)
    assert not worker.run_once(task['id'])


def test_library_worker_cannot_generate_from_half_a_required_group(tmp_path):
    from app.knowledge_worker import KnowledgeWorker
    from tests.test_question_jobs import LocalRuntime
    env,service,task=library_task(tmp_path,cap=1)
    def forbidden(*args):raise AssertionError('不得对半组证据发起生成')
    env.model.handler=forbidden
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    row=service.present(service.require_task(task['id']))
    assert row['status']=='succeeded' and row['result']['status']=='insufficient_evidence'
    assert row['requests_used']==1 and not env.model.calls
    assert row['result']['retrieval']['math_evidence']['incomplete_count']==1


def test_business_value_cannot_be_backed_by_an_index_through_production_contract():
    from app.rag_answer_contract import validate_task_output,ANSWER_CONTRACT_VERSION
    from app.rag_validation import ModelOutputError
    from app.rag_context import EvidencePack
    from app.rag_extract_context import assign_unit_options
    item=EvidenceItem(1,'c','d','v',1,'推论7：k_1<k_2。',segment_policy='sentence-segments-v2')
    item.read_ranges=[[0,len(item.text)]]
    assign_unit_options([item],[])
    pack=EvidencePack([item],[item],None,'v',False,100)
    output=dict(status='answered',conclusion=[dict(text='成本1元。',claim_type='direct',
        context=[dict(kind='unit',ref=1,span=1,segment=1)],evidence=[dict(ref=1,unit=1)],part_ids=['p1'])],explanation=[],
        clarification_questions=[],missing_points=[],answer_parts=[dict(part_id='p1',state='answered',missing_detail='')])
    with pytest.raises(ModelOutputError,match='number_not_in_quote'):
        output['presentation']={'blocks':[dict(role='evidence',kind='paragraph',fact_indices=[0])]}
        validate_task_output(json.dumps(output,ensure_ascii=False),pack,question='成本是多少',contract_version=ANSWER_CONTRACT_VERSION)
