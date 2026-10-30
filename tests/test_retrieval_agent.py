"""有界检索工程协议：部分测试注入首查窗口缺口，不是封存语义题或检索准确率。"""
from dataclasses import replace
from copy import deepcopy
import json

import pytest

from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from app.knowledge_traces import KnowledgeTraces
from app.product_access import ProductAccess
from app.repository import now_iso
from app.schemas import Document,Chunk,SourceLocation
from app.retrieval_agent import decision,observed,remaining_requests,policy
from app.rag_context import EvidenceItem,EvidencePack
from app.rag_validation import ModelOutputError
from tests.test_question_jobs import question_fixture,LocalRuntime


TEXT='受理需出示凭证。遗失凭证时须先核验登记。'


def test_legacy_trace_reads_recorded_usage_without_leaking_private_metadata(tmp_path):
    from app.question_worker import QuestionWorker
    from tests.test_question_jobs import submit
    env=question_fixture(tmp_path)
    job,_=submit(env)
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    assert env.service.get_job(job['id'])['status']=='succeeded'
    # 合成历史记录包含额外字段，公开链路仍只能投影合法计数；未知用量不能变成0。
    with env.repository.connect() as db:
        db.execute('UPDATE question_calls SET provider_usage_json=? WHERE job_id=? AND role=?',
            (json.dumps({'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10,
                'cached_tokens':0,'reasoning_tokens':True,'private':'fixture-private-usage'},
                'raw':'fixture-private-response'}),job['id'],'generation'))
    trace=KnowledgeTraces(env.repository).detail(job['id'])
    by_role={call['role']:call for call in trace['calls']}
    assert by_role['generation']['usage']==dict(prompt_tokens=7,completion_tokens=3,total_tokens=10,cached_tokens=0)
    assert by_role['embedding']['usage'] is None
    assert all(call['status']=='succeeded' and call['delivery_status']=='sent' for call in trace['calls'])
    serialized=json.dumps(trace)
    assert 'usage_json' not in serialized and 'fixture-private' not in serialized
    assert all('usage' not in span and 'delivery_status' not in span for span in trace['spans'])
    assert (len(env.embedding.calls),len(env.model.calls))==(1,1)


def choice(action='finish', *, state='candidate', refs=None, query='', plan=None):
    return dict(action=action,part_ids=['p1'] if action!='finish' else [],
                gap='核对缺少的适用条件' if action not in ('finish','ask_clarification') else '',query=query,
                refs=refs or [],coverage=[dict(part_id='p1',state=state,refs=[] if state=='missing' else [1])],plan=plan)


def setup(tmp_path,monkeypatch,*,gap=False,headings=False):
    env=question_fixture(tmp_path,retrieval_agent_enabled=True)
    env.repository.create(Document(id='agent-source',filename='隔离规则.txt',size=100,created_at=now_iso(),status='uploaded',format='txt'))
    chunks=[Chunk(id='agent-c1',document_id='agent-source',text=TEXT,heading_path='业务规范 > 受理',
        parse_version_id='agent-v1',order_index=1,chunk_type='text',sources=[SourceLocation(format='txt',line_start=2,line_end=2,section_path='业务规范 > 受理')])]
    if headings:chunks.insert(0,Chunk(id='agent-title',document_id='agent-source',text='业务规范',
        parse_version_id='agent-v1',order_index=0,chunk_type='section_header',sources=[SourceLocation(format='txt',line_start=1,line_end=1)]))
    env.repository.finish_parse('agent-source',1,chunks)
    env.rag.index.build('agent-source')
    access=ProductAccess(env.repository,env.settings,env.rag.index)
    base=access.create_base('Agent工程夹具','仅合成来源',collection_name='agent-fixture')
    access.bind_document(base['id'],'agent-source');access.publish(base['id'],0)
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'agent-conversation')
    env.embedding.calls.clear();env.model.calls.clear()
    if gap:
        import app.rag_pipeline as pipeline
        original=pipeline.knowledge_context
        def inject(*args,**kwargs):
            pack,report=original(*args,**kwargs)
            from app.rag_extract_context import assign_unit_options
            pack.items=[i for i in pack.items if i.chunk_type!='section_header']
            for n,item in enumerate(pack.items,1):item.reference_id=n
            for item in pack.items:
                item.read_ranges=[[0,len('受理需出示凭证。')]];item.unit_choices=None
            assign_unit_options(pack.items,pack.continuation_pairs)
            return pack,report
        monkeypatch.setattr(pipeline,'knowledge_context',inject)
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    return env,access,service,conv,worker


def body(user):
    values=json.loads(user);facts=[]
    for ref in values['references']:
        text=''.join(s for span in ref['content'] for s in span['segments'].values())
        facts.append(dict(text=text,claim_type='direct',context=[],part_ids=['p1'],
                          evidence=[dict(ref=ref['ref'],unit=int(u)) for u in ref['quote_units']]))
    unresolved=any(c['state']=='partial' for c in values.get('retrieval_obligations',[]))
    detail='仍有待补证的适用条件' if unresolved else ''
    return json.dumps(dict(status='partial' if unresolved else 'answered',conclusion=facts,explanation=[],clarification_questions=[],
        missing_points=[detail] if unresolved else [],answer_parts=[dict(part_id='p1',state='partial' if unresolved else 'answered',missing_detail=detail)]),ensure_ascii=False)


def execute(service,conv,worker):
    task=service.submit(conv['id'],'受理有什么要求？','agent-turn')
    assert task['provider_requests_expected']==5
    assert worker.run_once(task['id'])
    return service.present(service.require_task(task['id']))


def test_first_finish_uses_one_decision_and_one_write_without_legacy_planner(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    env.model.handler=lambda s,u:json.dumps(choice()) if 'agent' in json.loads(u) else body(u)
    result=execute(service,conv,worker)
    assert result['status']=='succeeded',result
    assert result['requests_used']==3 and len(env.embedding.calls)==1 and len(env.model.calls)==2
    assert result['retrieval_agent']['stop_reason']=='finished'
    assert not result['retrieval_agent']['semantic_completeness_verified']
    assert service.submit(conv['id'],'受理有什么要求？','agent-turn')['id']==result['id']
    assert len(env.model.calls)==2


def test_read_source_adds_actual_hidden_window_and_keeps_first_conditions(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch,gap=True)
    def respond(s,u):
        value=json.loads(u)
        if 'agent' not in value:return body(u)
        return json.dumps(choice('read_source',state='partial',refs=[1]) if value['agent']['round']==0 else choice())
    env.model.handler=respond;result=execute(service,conv,worker)
    assert result['status']=='succeeded',result
    summary=result['retrieval_agent'];steps=summary['steps']
    assert len(steps)==2 and steps[0]['after']['visible_chars']>steps[0]['before']['visible_chars']
    assert result['requests_used']==4 and len(env.embedding.calls)==1 and len(env.model.calls)==3
    assert TEXT==''.join(c['quote'] for c in result['result']['citations'])
    trace=KnowledgeTraces(env.repository).detail(result['id'])
    assert any(s['kind']=='AGENT_READ' and s['status']=='succeeded' for s in trace['spans'])
    assert '核对缺少的适用条件' not in json.dumps(trace,ensure_ascii=False)


def test_read_same_full_source_stops_without_second_decision(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    env.model.handler=lambda s,u:json.dumps(choice('read_source',state='partial',refs=[1])) if 'agent' in json.loads(u) else body(u)
    result=execute(service,conv,worker)
    assert result['status']=='succeeded',result
    assert result['retrieval_agent']['stop_reason']=='no_new_evidence'
    assert result['result']['status']=='partial'
    assert result['requests_used']==3 and len(env.model.calls)==2 and len(env.embedding.calls)==1


@pytest.mark.parametrize('action',['search_keyword','search_vector'])
def test_supplement_search_uses_at_most_one_extra_query(tmp_path,monkeypatch,action):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch,gap=True)
    def respond(s,u):
        value=json.loads(u)
        if 'agent' not in value:return body(u)
        return json.dumps(choice(action,state='partial',query='遗失凭证核验登记') if value['agent']['round']==0 else choice())
    env.model.handler=respond;result=execute(service,conv,worker)
    assert result['status']=='succeeded',result
    expected=2 if action=='search_vector' else 1
    assert len(env.embedding.calls)==expected and result['requests_used']==3+expected
    assert len(result['retrieval_agent']['steps'])==2


def test_cancel_during_decision_stops_before_source_read_or_write(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch,gap=True)
    task=service.submit(conv['id'],'受理有什么要求？','agent-turn')
    def respond(s,u):
        with env.repository.connect() as db:
            db.execute('UPDATE kb_tasks SET cancel_requested=1 WHERE id=?',(task['id'],))
        return json.dumps(choice('read_source',state='partial',refs=[1]))
    env.model.handler=respond;worker.run_once(task['id']);row=worker.tasks.row(task['id'])
    assert row['status']=='cancelled' and row['result_json'] is None
    assert len(env.model.calls)==1 and len(env.embedding.calls)==1


def test_source_change_during_decision_stops_before_any_followup(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch,gap=True)
    def respond(s,u):
        with env.repository.connect() as db:db.execute("UPDATE chunks SET text=text||'变更' WHERE id='agent-c1'")
        return json.dumps(choice('read_source',state='partial',refs=[1]))
    env.model.handler=respond
    task=service.submit(conv['id'],'受理有什么要求？','agent-turn');worker.run_once(task['id'])
    row=worker.tasks.row(task['id'])
    assert row['status']=='failed' and row['error_code']=='source_changed' and row['result_json'] is None
    assert len(env.model.calls)==1 and len(env.embedding.calls)==1


def test_frozen_request_budget_refuses_before_first_query(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    task=service.submit(conv['id'],'受理有什么要求？','agent-turn')
    with env.repository.connect() as db:db.execute('UPDATE kb_tasks SET max_requests=4 WHERE id=?',(task['id'],))
    worker.run_once(task['id']);row=worker.tasks.row(task['id'])
    assert row['status']=='failed' and row['requests_used']==0
    assert not env.model.calls and not env.embedding.calls


@pytest.mark.parametrize('mutation',[
    {'action':'query_registered_tool'}, {'action':'shell'}, {'refs':[True]}, {'refs':[999]},
    {'part_ids':['p999']}, {'query':'x'*161}, {'path':'D:/private'},
    {'coverage':[{'part_id':'p1','state':'missing','refs':[1]}]},
])
def test_invalid_actions_and_scope_suggestions_fail_closed(mutation):
    pack=unit_pack()
    value=choice('read_source',state='partial',refs=[1]);value.update(mutation)
    with pytest.raises(ModelOutputError):decision(value,pack,'要求是什么？',0,'fixed-input')


def test_second_read_and_changed_checkpoint_are_rejected():
    pack=unit_pack()
    with pytest.raises(ModelOutputError):decision(choice('read_source',state='partial',refs=[1]),pack,'要求是什么？',1,'fixed')
    with pytest.raises(ModelOutputError):decision({'input_hash':'other','decision':choice()},pack,'要求是什么？',0,'fixed')


def test_independent_optional_model_stages_cannot_silently_stack(tmp_path):
    from app.config import Settings
    assert policy(Settings(data_dir=tmp_path))['enabled'] is False
    for extra in ('query_planner_enabled','answer_semantic_review_enabled'):
        with pytest.raises(ValueError,match='不能叠加'):
            Settings(data_dir=tmp_path,retrieval_agent_enabled=True,**{extra:True})


def unit_pack():
    items=[EvidenceItem(1,'c','d','v',1,'合成正文。')]
    return EvidencePack(items=items,candidates=items,index_id='i',parse_version_id='v',truncated=False,context_chars=10)


def test_repeating_original_vector_query_never_sends_it_again(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch,gap=True)
    task=service.submit(conv['id'],'受理有什么要求？','agent-turn')
    question=json.loads(worker.tasks.row(task['id'])['input_json'])['retrieval_question']
    env.model.handler=lambda s,u:json.dumps(choice('search_vector',state='partial',query=question)) if 'agent' in json.loads(u) else body(u)
    worker.run_once(task['id']);result=service.present(service.require_task(task['id']))
    assert result['status']=='succeeded',result
    assert result['retrieval_agent']['stop_reason']=='repeated_action'
    assert len(env.embedding.calls)==1 and len(env.model.calls)==2


def test_complex_agent_finish_reuses_original_composition_validation(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    def respond(s,u):
        value=json.loads(u)
        if 'agent' in value:
            units=value['evidence_units'];first=next(x for x in units if '受理需' in ''.join(p['text'] for p in x['passages']))
            second=next(x for x in units if '遗失' in ''.join(p['text'] for p in x['passages']))
            blocks=[dict(role='conclusion',focus='受理要求',part_ids=['p1'],groups=[dict(label='受理',units=[{k:first[k] for k in ('ref','unit')}])]),
                    dict(role='boundary',focus='凭证遗失处理',part_ids=['p2'],groups=[dict(label='遗失',units=[{k:second[k] for k in ('ref','unit')}])])]
            result=choice(plan={'blocks':blocks,'missing_part_ids':[]})
            result['coverage']=[dict(part_id=p,state='candidate',refs=[1]) for p in ('p1','p2')]
            return json.dumps(result)
        facts=[dict(text='受理需出示凭证。',claim_type='direct',context=[],part_ids=['p1'],evidence=[dict(ref=1,unit=1)]),
               dict(text='遗失凭证时须先核验登记。',claim_type='direct',context=[],part_ids=['p2'],evidence=[dict(ref=1,unit=2)])]
        return json.dumps(dict(status='answered',conclusion=facts[:1],explanation=facts[1:],clarification_questions=[],missing_points=[],
            answer_parts=[dict(part_id=p,state='answered',missing_detail='') for p in ('p1','p2')],
            presentation={'blocks':[dict(role=r,kind='paragraph',fact_indices=[i]) for i,r in enumerate(('conclusion','boundary'))]}))
    env.model.handler=respond
    task=service.submit(conv['id'],'受理需要什么？遗失凭证时如何处理？','two-parts');worker.run_once(task['id'])
    result=service.present(service.require_task(task['id']))
    assert result['status']=='succeeded',result
    assert result['requests_used']==3 and len(env.model.calls)==2
    assert 'composition_plan' in json.loads(env.model.calls[-1][1])


def test_validated_decisions_survive_explicit_retry_without_repeating_search(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch,gap=True)
    writes=[]
    def respond(s,u):
        value=json.loads(u)
        if 'agent' in value:return json.dumps(choice('read_source',state='partial',refs=[1]) if value['agent']['round']==0 else choice())
        writes.append(1)
        if len(writes)==1:
            from app.deepseek import ModelError
            error=ModelError(429,'synthetic-before-send');error.not_sent=True;raise error
        return body(u)
    env.model.handler=respond
    task=service.submit(conv['id'],'受理有什么要求？','retry-case');worker.run_once(task['id'])
    first=worker.tasks.row(task['id']);assert first['status']=='failed' and first['error_code']=='not_sent'
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(env.settings)) as client:
        response=client.post('/api/kb-tasks/'+task['id']+'/retry',json={'acknowledge_uncertain':False})
        assert response.status_code==200,response.text
    worker.run_once(task['id']);result=service.present(service.require_task(task['id']))
    assert result['status']=='succeeded',result
    assert result['requests_used']==4 and len(env.embedding.calls)==1 and len(env.model.calls)==4
    with env.repository.connect() as db:
        calls=[dict(r) for r in db.execute('SELECT * FROM kb_task_calls WHERE task_id=? ORDER BY sequence_no',(task['id'],))]
    assert [r['status'] for r in calls]==['succeeded','succeeded','succeeded','failed','succeeded']
    assert calls[3]['delivery_status']=='not_sent'


def test_uncertain_decision_requires_explicit_ack_and_does_not_auto_run(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    def respond(s,u):
        from app.deepseek import ModelError
        raise ModelError(504,'synthetic-timeout')
    env.model.handler=respond
    task=service.submit(conv['id'],'受理有什么要求？','timeout-case');worker.run_once(task['id'])
    row=worker.tasks.row(task['id']);assert row['status']=='needs_attention'
    assert worker.run_once(task['id']) is False
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(env.settings)) as client:
        response=client.post('/api/kb-tasks/'+task['id']+'/retry',json={'acknowledge_uncertain':False})
        assert response.status_code==409
    assert len(env.embedding.calls)==1 and len(env.model.calls)==1


@pytest.mark.parametrize('action',['read_neighbors','read_heading_path'])
def test_reading_uses_real_neighbor_or_heading_chunks_from_same_snapshot(tmp_path,monkeypatch,action):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch,gap=True,headings=True)
    def respond(s,u):
        value=json.loads(u)
        if 'agent' not in value:return body(u)
        return json.dumps(choice(action,state='partial',refs=[1]) if value['agent']['round']==0 else choice())
    env.model.handler=respond;result=execute(service,conv,worker)
    assert result['status']=='succeeded',result
    assert len(result['retrieval_agent']['steps'])==2 and len(env.embedding.calls)==1
    assert '业务规范' in ''.join(c['quote'] for c in result['result']['citations'])
    assert {c['document_id'] for c in result['result']['citations']}=={'agent-source'}


def test_heading_metadata_without_raw_indexed_heading_stays_missing(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    env.model.handler=lambda s,u:json.dumps(choice('read_heading_path',state='partial',refs=[1])) if 'agent' in json.loads(u) else body(u)
    result=execute(service,conv,worker)
    assert result['status']=='succeeded',result
    assert result['retrieval_agent']['stop_reason']=='no_new_evidence'
    assert '业务规范' not in ''.join(c['quote'] for c in result['result']['citations'])


def test_revocation_after_decision_cancels_without_more_calls(tmp_path,monkeypatch):
    env,access,service,conv,worker=setup(tmp_path,monkeypatch)
    task=service.submit(conv['id'],'受理有什么要求？','revoked-case')
    def respond(s,u):
        with env.repository.connect() as db:db.execute('UPDATE knowledge_bases SET revoked_at=? WHERE id=?',(now_iso(),conv['base_id']))
        return json.dumps(choice('read_source',state='partial',refs=[1]))
    env.model.handler=respond;worker.run_once(task['id']);row=worker.tasks.row(task['id'])
    assert row['status']=='cancelled' and row['result_json'] is None
    assert len(env.model.calls)==1 and len(env.embedding.calls)==1


def test_general_auto_reply_does_not_enter_enabled_document_agent(tmp_path):
    from app.unified_chat import UnifiedChat
    from tests.test_general_chat import send
    env=question_fixture(tmp_path,retrieval_agent_enabled=True)
    access=ProductAccess(env.repository,env.settings,env.rag.index);service=KnowledgeQuestions(access,env.rag);chat=UnifiedChat(access,service)
    env.model.handler=lambda s,u:json.dumps({'kind':'general','answer':'隔离通用回复'})
    conv,task=send(chat,service)
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id']);turn=chat.detail(conv['id'])['turns'][0]
    assert turn['result']['response_kind']=='general' and turn.get('retrieval_agent') is None
    assert len(env.model.calls)==1 and len(env.embedding.calls)==0


def test_actual_sdk_decisions_and_stream_writer_share_one_persistent_token_task(tmp_path,monkeypatch):
    import httpx
    from openai import OpenAI
    from app.deepseek import DeepSeekModel
    from tests.test_question_jobs import current_protocol
    env,_,service,conv,worker=setup(tmp_path,monkeypatch,gap=True)
    requests=[]
    def respond(request):
        wire=json.loads(request.content);requests.append(wire);value=json.loads(wire['messages'][1]['content'])
        text=json.dumps(choice('read_source',state='partial',refs=[1]) if value['agent']['round']==0 else choice()) if 'agent' in value else current_protocol(body(wire['messages'][1]['content']))
        usage={'prompt_tokens':40,'completion_tokens':10,'total_tokens':50}
        if wire.get('stream'):
            chunks=[dict(id='sdk-fixture',object='chat.completion.chunk',created=0,model=wire['model'],
                         choices=[dict(index=0,delta={'content':text},finish_reason=None)]),
                    dict(id='sdk-fixture',object='chat.completion.chunk',created=0,model=wire['model'],
                         choices=[dict(index=0,delta={},finish_reason='stop')],usage=usage)]
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
        return httpx.Response(200,json=dict(id='sdk-fixture',object='chat.completion',created=0,model=wire['model'],
            choices=[dict(index=0,message={'role':'assistant','content':text},finish_reason='stop')],usage=usage))
    monkeypatch.setattr('app.deepseek.OpenAI',lambda **kw:OpenAI(http_client=httpx.Client(transport=httpx.MockTransport(respond)),**kw))
    worker.model=DeepSeekModel(env.settings)
    result=execute(service,conv,worker)
    assert result['status']=='succeeded',result
    assert len(requests)==3 and requests[-1]['stream'] is True
    with env.repository.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM context_reservations')]
    assert len(rows)==3 and {r['task_key'] for r in rows}=={'knowledge:'+result['id']}
    assert [r['profile'] for r in rows]==['planning','planning','answer']
    assert all(r['state']=='reported' and r['charged_units']==50 for r in rows)
    trace=KnowledgeTraces(env.repository).detail(result['id'])
    assert sum(c.get('usage',{}).get('total_tokens',0) for c in trace['calls'] if c.get('usage'))==150
    assert 'usage_json' not in json.dumps(trace)


def test_merge_preserves_math_warnings_and_refuses_dropping_first_conditions():
    from app.retrieval_agent import merge
    first=unit_pack();first.math_hints={'blocked_count':2,'unlinked_formula_count':1}
    item=EvidenceItem(1,'other','d','v',1,'新片段。')
    extra=EvidencePack(items=[item],candidates=[item],index_id='i',parse_version_id='v',truncated=False,context_chars=10)
    docs=[{'document_id':'d','filename':'合成.txt'}]
    merged,reason=merge(first,[extra],docs,{'context_chars':10000,'context_limit':2})
    assert reason is None and merged.math_hints['blocked_count']==2 and merged.math_hints['unlinked_formula_count']==1
    limited,reason=merge(first,[extra],docs,{'context_chars':10,'context_limit':1})
    assert limited is first and reason=='context_budget'


def test_bad_agent_output_keeps_private_first_response_and_exposes_only_safe_diagnostic(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    raw=json.dumps({**choice(),'action':'shell','private_probe':'fixture-private-marker'})
    env.model.handler=lambda s,u:raw
    task=service.submit(conv['id'],'受理有什么要求？','bad-decision');worker.run_once(task['id'])
    row=worker.tasks.row(task['id']);assert row['status']=='failed' and row['requests_used']==2
    with env.repository.connect() as db:
        diagnostic=json.loads(db.execute("SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name LIKE 'diagnostic_validation_%'",(task['id'],)).fetchone()[0])
    assert diagnostic['output']==raw and diagnostic['reason']=='agent_decision_invalid'
    public=KnowledgeTraces(env.repository).detail(task['id'])
    assert 'fixture-private-marker' not in json.dumps(public) and raw not in json.dumps(public)
    assert len(env.model.calls)==1 and len(env.embedding.calls)==1


def test_all_missing_parts_end_without_a_writer_request(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    env.model.handler=lambda s,u:json.dumps(choice(state='missing'))
    result=execute(service,conv,worker)
    assert result['status']=='succeeded' and result['result']['status']=='insufficient_evidence'
    assert result['retrieval_agent']['stop_reason']=='no_supported_parts'
    assert len(env.model.calls)==1 and len(env.embedding.calls)==1 and result['requests_used']==2


def test_writer_cannot_erase_unresolved_agent_coverage(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    def respond(s,u):
        value=json.loads(u)
        if 'agent' in value:return json.dumps(choice('read_source',state='partial',refs=[1]))
        value.pop('retrieval_obligations')
        return body(json.dumps(value))
    env.model.handler=respond;task=service.submit(conv['id'],'受理有什么要求？','lost-gap');worker.run_once(task['id'])
    row=worker.tasks.row(task['id'])
    assert row['status']=='failed' and row['error_code']=='output_agent_coverage_lost' and row['result_json'] is None
    assert len(env.model.calls)==2 and len(env.embedding.calls)==1


def test_complex_clarification_does_not_require_an_invented_composition_plan(tmp_path,monkeypatch):
    env,_,service,conv,worker=setup(tmp_path,monkeypatch)
    response=choice('ask_clarification',state='missing',query='请明确要核对的受理对象。')
    response['coverage']=[dict(part_id=p,state='missing',refs=[]) for p in ('p1','p2')]
    env.model.handler=lambda s,u:json.dumps(response)
    task=service.submit(conv['id'],'受理需要什么？遗失凭证时如何处理？','clarify-case');worker.run_once(task['id'])
    result=service.present(service.require_task(task['id']))
    assert result['status']=='succeeded' and result['result']['status']=='clarification_needed'
    assert result['requests_used']==2 and len(env.model.calls)==1 and len(env.embedding.calls)==1
