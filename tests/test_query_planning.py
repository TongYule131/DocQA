"""S0/S1/S2 定向验收：合成原文、真实规划/装入/任务链路，供应商使用桩。"""
from dataclasses import replace
import json

import pytest

from app.query_planning import plan_query, execution_budget, public_plan
from app.config import Settings
from app.rag_pipeline import knowledge_context, RetrievalBudget
from app.rag_source_diagnostics import source_diagnostics, public_source_diagnostics
from app.retrieval_planning import plan_retrieval
from tests.test_retrieval_planning import chunk, vector_result
from tests.test_product_access import team


@pytest.mark.parametrize('question,kind,requirements', [
    ('这份文档讲了什么', 'document_overview', {'overview_abstract','overview_body','author_position'}),
    ('比较两种机制的适用条件', 'comparison', set()),
    ('两组概念是否一一对应', 'conceptual', {'direct_definition','mechanism_body'}),
    ('该说法是否符合论文观点，是否意味着不需要治理', 'boundary', {'author_position','boundary_conditions'}),
    ('请核对公式与正文解释是否一致', 'formula_check', {'formula_source','formula_explanation'}),
    ('期限是多少？有什么例外？', 'multi_hop', set()),
    ('退货条件是什么', 'fact', set()),
])
def test_automatic_task_plan_is_not_a_per_document_demo(question,kind,requirements):
    plan=plan_query(question)
    assert plan['task_type']==kind and set(plan['requirements'])==requirements
    assert plan['supplier_calls']==0 and 'subquestions' not in public_plan(plan)
    assert plan['scope_policy']=='authorized_selected_snapshot'


def test_budget_is_shared_and_business_node_can_only_tighten_context():
    settings=Settings(question_candidate_k=80,question_context_k=40)
    assert execution_budget(plan_query('概述全文'),settings)['candidate_limit']==80
    assert execution_budget(plan_query('普通事实'),settings)['context_limit']==5
    assert execution_budget(plan_query('核对公式与正文是否一致'),settings,{'top_k':3})['context_limit']==3
    assert execution_budget(plan_query('你好',{'kind':'SYSTEM'}),settings)=={}


def test_overview_phrase_reads_all_small_index_and_never_claims_large_index_complete():
    chunks=[chunk(0,'设备文档研究接口协作。','摘要'),chunk(1,'接口必须先验证签名。','1 机制'),
        chunk(2,'最终结论是保留验证步骤。','2 结论')]
    planned=plan_retrieval('这份文档讲了什么',vector_result(chunks[:1]),chunks,max_items=4,fulltext_budget=12000)
    assert planned['planning']['route']=='complete_indexed_text'
    assert {i['chunk_id'] for i in planned['results']}=={'c0','c1','c2'}
    small=plan_retrieval('这份文档讲了什么',vector_result(chunks[:1]),chunks,max_items=1,fulltext_budget=12000)
    assert small['planning']['route']!='complete_indexed_text'
    assert {'overview_abstract','overview_section','author_position'} <= {s['kind'] for s in small['planning']['evidence_slots']}


def pipeline(tmp_path, *, context_limit=3, context_chars=12000, formula=True):
    from tests.test_question_jobs import question_fixture
    from app.schemas import Document,Chunk,SourceLocation
    from app.repository import now_iso
    env=question_fixture(tmp_path)
    specs=[('first',[('f1','系统简介。','摘要'),('f2','一般说明。','1 概览')]),
        ('second',[('s1','推论7解释：在该条件下，宣传水平高于另一情形。','3 解释')]+(
            [('s2','推论7：k_A < k_B，条件为同一投入水平。','3 解释')] if formula else []))]
    docs=[];hits={}
    for name,rows in specs:
        env.repository.create(Document(id=name,filename=name+'.txt',format='txt',size=100,created_at=now_iso(),status='uploaded'))
        chunks=[Chunk(id=identifier,document_id=name,text=text,heading_path=heading,parse_version_id=name+'-v',
            chunk_type='text',order_index=n,sources=[SourceLocation(format='txt',line_start=n+1,line_end=n+1)]) for n,(identifier,text,heading) in enumerate(rows)]
        env.repository.finish_parse(name,1,chunks)
        docs.append(dict(document_id=name,filename=name+'.txt',snapshot=dict(parse_version_id=name+'-v')))
        hits[name]=dict(index_id=name+'-idx',parse_version_id=name+'-v',results=[
            dict(chunk_id=c.id,document_id=name,parse_version_id=c.parse_version_id,text=c.text,heading_path=c.heading_path,
                order_index=c.order_index,chunk_type=c.chunk_type,sources=[s.model_dump() for s in c.sources],score=.99-n*.01)
            for n,c in enumerate(chunks)])
    class Index:
        def search_snapshot(self,document_id,question,k,**kwargs):return hits[document_id]
    question='请核对推论7的公式与正文宣传解释是否一致'
    pack,report=knowledge_context(Index(),env.repository,docs,question,question,env.settings,
        RetrievalBudget(8,64,context_limit,context_chars))
    return env,pack,report


def test_global_packing_keeps_both_formula_and_explanation_even_in_second_document(tmp_path):
    env,pack,report=pipeline(tmp_path,context_limit=2)
    assert {i.chunk_id for i in pack.items}=={'s1','s2'}
    assert len(pack.candidates)==4 and len(pack.items)==2
    assert report['slots']['unfilled_count']==0
    assert not report['slots']['semantic_completeness_verified']
    assert report['budget']['candidate_limit']==64
    assert report['coverage']['route']=='selected_evidence'
    assert report['context_chars']<=12000
    assert [i.reference_id for i in pack.items]==[1,2]
    assert not env.embedding.calls and not env.model.calls


def test_formula_missing_and_budget_dropped_are_different_states(tmp_path):
    _,_,missing=pipeline(tmp_path/'missing',formula=False)
    formula=next(s for s in missing['slots']['slots'] if s['kind']=='formula_source')
    assert formula['state']=='no_candidate' and missing['slots']['unfilled_count']>0
    _,_,limited=pipeline(tmp_path/'limited',context_limit=1)
    assert any(s['state']=='not_admitted' for s in limited['slots']['slots'])
    assert all(s['candidate_count']>0 for s in limited['slots']['slots'])


def test_source_diagnostics_separate_addresses_from_values_without_rewriting():
    original='第8页，推论7规定收益为63.7万元。\ufffd'
    item=chunk(0,original)
    report=source_diagnostics([item],None)
    assert item.text==original
    assert report['locator_counts']=={'page':1,'推论':1}
    assert report['source_value_count']==1 and report['replacement_chars']==1
    assert not report['ocr_accuracy_verified'] and not report['formula_semantics_verified']
    assert report['regions'][0]['locators'][0]==dict(start=0,end=3,kind='page')
    assert original not in json.dumps(public_source_diagnostics(report),ensure_ascii=False)
    assert 'regions' not in public_source_diagnostics(report)


def test_page_locator_cannot_satisfy_numeric_evidence_slot():
    from app.rag_evidence_slots import evidence_slots
    pool=vector_result([chunk(0,'第37页讨论投入成本。')])['results']
    slots=evidence_slots('投入成本为37万元吗',pool)
    assert slots and slots[0]['kind']=='numeric_source' and slots[0]['candidate_ids']==[]
    page_slots=evidence_slots('请看第37页',pool)
    assert len(page_slots)==1 and page_slots[0]['kind']=='source_page'
    assert page_slots[0]['candidate_ids']==[]  # 正文提到37页不能代替真实来源地址，也不能生成numeric_source槽。


def test_task_freezes_plan_budget_and_candidate_change_stops_before_provider(tmp_path):
    from tests.test_knowledge_chain import library
    from tests.test_question_jobs import LocalRuntime
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    env,access,base=library(tmp_path)
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'c')
    task=service.submit(conv['id'],'这份文档讲了什么','q')
    row=service.require_task(task['id']);payload=json.loads(row['input_json'])
    assert payload['query_plan']['task_type']=='document_overview'
    assert payload['retrieval_budget']['candidate_limit']==64
    changed=replace(env.settings,question_candidate_k=80)
    worker=KnowledgeWorker(changed,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    assert worker.tasks.row(task['id'])['error_code']=='configuration_changed'
    assert not env.embedding.calls and not env.model.calls


def test_successful_worker_records_plan_real_coverage_and_private_safe_trace(tmp_path):
    from tests.test_knowledge_chain import library
    from tests.test_question_jobs import LocalRuntime
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    from app.knowledge_traces import KnowledgeTraces
    env,access,base=library(tmp_path)
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'c')
    def reply(system,user):
        entry=next(i for i in json.loads(user)['references'] if '保留包装' in str(i['content']))
        return json.dumps(dict(status='answered',conclusion=[dict(text='退货需要保留包装。',claim_type='direct',context=[],
            evidence=[dict(ref=entry['ref'],unit=1)],part_ids=['p1'])],explanation=[],clarification_questions=[],
            missing_points=[],answer_parts=[dict(part_id='p1',state='answered',missing_detail='')]),ensure_ascii=False)
    env.model.handler=reply
    task=service.submit(conv['id'],'SYNTHETIC-PRIVATE-QUESTION：退货有什么要求','q')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    row=worker.tasks.row(task['id'])
    assert row['status']=='succeeded',row
    assert row['requests_used']==2
    prompt=json.loads(env.model.calls[0][1])
    assert prompt['query_plan']['task_type']=='fact'
    assert '本轮回答组织策略' in env.model.calls[0][0]
    assert sum(len(value) for value in env.model.calls[0])<=env.settings.question_input_max_chars
    trace=KnowledgeTraces(env.repository).detail(task['id'])
    assert trace['query_plan']['task_type']=='fact'
    assert any(s['kind']=='QUERY_PLAN' for s in trace['spans'])
    diagnostic=next(d for d in trace['diagnostics'] if d['kind']=='retrieval')
    assert diagnostic['sources'] and diagnostic['budget']['candidate_limit']==64
    assert diagnostic['context_chars']==len(json.dumps(prompt['references'],ensure_ascii=False,sort_keys=True,separators=(',',':')))
    assert 'SYNTHETIC-PRIVATE-QUESTION' not in json.dumps(trace,ensure_ascii=False)
    assert not worker.run_once(task['id']) and len(env.model.calls)==1


def test_admin_preview_uses_same_plan_and_marks_unpublished_scope_without_calls(team):
    env,_,clients=team;admin=clients['admin']
    base=admin.post('/api/knowledge-bases',json={'name':'尚未发布的合成库'}).json()
    response=admin.post('/api/admin/intents/test',json={'scope':'knowledge','base_id':base['id'],
        'questions':['这份文档讲了什么','核对公式与正文是否一致','你好']})
    assert response.status_code==200,response.text
    results=response.json()['results']
    assert [r['query_plan']['task_type'] for r in results]==['document_overview','formula_check','interaction']
    assert results[0]['retrieval_budget']['candidate_limit']==64
    assert not results[0]['availability']['ready'] and results[2]['availability']['ready']
    assert clients['alice'].post('/api/admin/intents/test',json={'questions':['你好']}).status_code==403
    with env.repository.connect() as db:assert db.execute('SELECT COUNT(*) FROM kb_tasks').fetchone()[0]==0
    assert not env.embedding.calls and not env.model.calls


def test_planning_does_not_expand_explicit_document_selection(tmp_path):
    from tests.test_knowledge_chain import library
    from app.knowledge_questions import KnowledgeQuestions
    env,access,base=library(tmp_path)
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'c')
    task=service.submit(conv['id'],'综合全部资料，核对公式与正文是否一致','q',document_ids=['policy'])
    payload=json.loads(service.require_task(task['id'])['input_json'])
    assert [d['document_id'] for d in payload['documents']]==['policy']
    assert payload['document_filter']==['policy']
    assert payload['query_plan']['scope_policy']=='authorized_selected_snapshot'
    assert not env.embedding.calls and not env.model.calls


def test_fixed_input_overhead_fails_before_query_embedding(tmp_path,monkeypatch):
    from tests.test_knowledge_chain import library
    from tests.test_question_jobs import LocalRuntime
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    env,access,base=library(tmp_path)
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'c')
    task=service.submit(conv['id'],'退货有什么要求','q')
    monkeypatch.setattr('app.rag_answer_strategy.strategy_instructions',lambda question:'X'*env.settings.question_input_max_chars)
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    row=worker.tasks.row(task['id'])
    assert row['error_code']=='input_budget_exceeded' and row['requests_used']==0
    assert not env.embedding.calls and not env.model.calls
