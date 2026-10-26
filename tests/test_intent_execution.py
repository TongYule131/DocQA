"""按动作、范围和实际调用验证意图；不使用封存语料或真实供应商。"""
import json
import httpx
import pytest
from fastapi import HTTPException
from app.intent_execution import request_requirements
from app.intent_management import IntentManagement
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from app.knowledge_tools import ToolRegistry
from app.unified_chat import UnifiedChat
from app.clarifications import resolve, cancel
from tests.test_knowledge_chain import library
from tests.test_question_jobs import LocalRuntime


@pytest.mark.parametrize('question,live',[
    ('查询今天的仓库库存。',True), ('告诉我当前账户余额。',True),
    ('查看最新物流状态。',True), ('调用库存接口查询商品。',True),
    ('查询今天会议的安排。',True), ('获取实时设备负载。',True),
    ('查看当前排班。',True), ('查询当前知识库中关于排班的规定。',False),
    ('解释实时库存的含义。',False), ('文档中如何实现实时库存查询？',False),
    ('不是查询当前余额，请解释账户制度。',False),
    ('不要调用工具，概括报告中的结论。',False),
    ('报告里说“查询今天库存”，这句话是什么意思？',False),
    ('比较两套方案当前的适用条件。',False),
    ('说明公式中当前时点的变量。',False),
])
def test_live_request_is_an_action_not_a_topic(question,live):
    assert request_requirements(question)['requires_live_source'] is live


def test_unconnected_live_request_stops_before_retrieval_and_mixed_request_asks_scope(tmp_path):
    env,access,base=library(tmp_path);q=KnowledgeQuestions(access,env.rag)
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    for index,(question,action,status) in enumerate([
        ('查询今天仓库库存。','source_unavailable','insufficient_evidence'),
        ('概括文档的主要结论；同时查询今天仓库库存。','split_request','clarification_needed'),
    ]):
        conv=q.create(base['id'],'conversation-'+str(index));task=q.submit(conv['id'],question,'ask')
        assert worker.run_once(task['id']);row=q.present(q.require_task(task['id']))
        assert row['status']=='succeeded',row
        assert row['result']['status']==status and row['result']['execution']['action']==action
        assert row['requests_used']==0 and not row['result']['citations']
        payload=json.loads(q.require_task(task['id'])['input_json']);assert not payload['documents']
    assert not env.embedding.calls and not env.model.calls


def test_knowledge_node_budget_and_document_scope_are_execution_constraints(tmp_path):
    env,access,base=library(tmp_path);manager=IntentManagement(env.repository)
    node=manager.save(dict(code='packing-policy',name='包装政策',kind='KB',base_id=base['id'],
                           examples=['退货需要哪些包装'],enabled=True,top_k=1))
    q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'selected',document_ids=['policy'])
    task=q.submit(conv['id'],'退货需要哪些包装','ask')
    payload=json.loads(q.require_task(task['id'])['input_json'])
    assert payload['execution']['action']=='retrieve'
    assert payload['execution']['resources']['document_ids']==['policy']
    assert payload['execution']['constraints']['top_k']==1 and payload['retrieval_budget']['context_limit']==1
    assert [d['document_id'] for d in payload['documents']]==['policy']
    assert not env.embedding.calls and not env.model.calls


def tool_fixture(tmp_path,monkeypatch):
    env,access,base=library(tmp_path)
    registry=ToolRegistry(env.repository)
    tool=registry.save(dict(id='stock-test',name='隔离仓储服务',endpoint='http://127.0.0.1:9999/mcp',
        tool_name='stock',auth_env=None,input_schema={'type':'object','properties':{
            'warehouse':{'type':'string','title':'仓库名称'},'product':{'type':'string','title':'商品编号'}},
            'required':['warehouse','product']},protocol_version='2025-11-25',read_only=True,enabled=True))
    node=IntentManagement(env.repository).save(dict(code='stock-query',name='库存查询',kind='MCP',
        base_id=base['id'],tool_id=tool['id'],examples=['查询当前库存'],enabled=True))
    calls=[]
    def http(request):
        body=json.loads(request.content);calls.append(body)
        if body['method']=='initialize':result={'protocolVersion':'2025-11-25'}
        elif body['method']=='notifications/initialized':return httpx.Response(202)
        else:result={'content':[{'type':'text','text':'隔离测试商品库存结果'}]}
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})
    client=httpx.Client
    monkeypatch.setattr('app.knowledge_tools.httpx.Client',lambda **kw:client(transport=httpx.MockTransport(http),**kw))
    env.model.handler=lambda *_:json.dumps({'warehouse':'仓甲','product':None})
    q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'conversation')
    task=q.submit(conv['id'],'查询当前库存','ask')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id']);row=q.present(q.require_task(task['id']))
    assert row['status']=='succeeded',row
    clar=row['result']['clarification'];assert clar['state']=='awaiting' and clar['fields'][0]['name']=='product'
    assert row['requests_used']==1 and not calls and not env.embedding.calls
    return env,access,base,q,conv,worker,clar,calls,node


def test_tool_missing_parameters_resume_original_tool_and_repeat_confirmation_does_not_call(tmp_path,monkeypatch):
    env,access,base,q,conv,worker,clar,calls,node=tool_fixture(tmp_path,monkeypatch)
    chat=UnifiedChat(access,q);identifier='k:'+conv['id']
    submitted=resolve(chat,identifier,clar['id'],['商品A'],'confirmed')
    assert submitted['status']=='submitted',submitted
    assert resolve(chat,identifier,clar['id'],['商品A'],'confirmed')['id']==submitted['id']
    assert len(env.model.calls)==1 and not calls
    with pytest.raises(HTTPException):resolve(chat,identifier,clar['id'],['商品B'],'another')
    def reply(system,user):
        value=json.loads(user)
        assert value['known_arguments']=={'warehouse':'仓甲','product':None}
        assert value['confirmed_fields']=={'product':'商品A'}
        return json.dumps({'warehouse':'仓甲','product':'商品A'})
    env.model.handler=reply
    assert worker.run_once(submitted['task_id'])
    row=q.present(q.require_task(submitted['task_id']));assert row['status']=='succeeded',row
    assert row['result']['tool_arguments']=={'warehouse':'仓甲','product':'商品A'}
    assert calls[-1]['params']['arguments']==row['result']['tool_arguments']
    assert len(calls)==3 and len(env.model.calls)==2 and not env.embedding.calls
    assert not row['result']['citations'] and row['requests_used']==4


@pytest.mark.parametrize('change',['cancel','memory','node','tool'])
def test_tool_confirmation_cannot_survive_invalidated_context(tmp_path,monkeypatch,change):
    env,access,base,q,conv,worker,clar,calls,node=tool_fixture(tmp_path,monkeypatch)
    chat=UnifiedChat(access,q);identifier='k:'+conv['id']
    if change=='cancel':cancel(chat,identifier,clar['id'])
    elif change=='memory':q.memory(conv['id'],False,True)
    else:
        with env.repository.connect() as db:
            if change=='node':db.execute('UPDATE intent_nodes SET enabled=0,revision=revision+1 WHERE id=?',(node['id'],))
            else:db.execute('UPDATE mcp_tools SET enabled=0,revision=revision+1 WHERE id=?',('stock-test',))
    try:
        value=resolve(chat,identifier,clar['id'],['商品A'],'confirmed')
        assert value['status']=='failed',value
    except HTTPException as exc:assert exc.status_code==409
    assert len(env.model.calls)==1 and not calls


@pytest.mark.parametrize('arguments',[{'warehouse':'别的仓库','product':'商品A'}, {'warehouse':'仓甲','product':'商品B'}])
def test_tool_continuation_cannot_change_an_already_confirmed_parameter(tmp_path,monkeypatch,arguments):
    env,access,base,q,conv,worker,clar,calls,node=tool_fixture(tmp_path,monkeypatch)
    submission=resolve(UnifiedChat(access,q),'k:'+conv['id'],clar['id'],['商品A'],'confirmed')
    env.model.handler=lambda *_:json.dumps(arguments)
    worker.run_once(submission['task_id']);row=q.present(q.require_task(submission['task_id']))
    assert row['status']=='failed' and not calls and len(env.model.calls)==2


def test_missing_credentials_are_detected_before_parameter_generation(tmp_path,monkeypatch):
    env,access,base=library(tmp_path);monkeypatch.delenv('DOCQA_MCP_ISOLATED_MISSING',raising=False)
    ToolRegistry(env.repository).save(dict(id='missing-credential',name='隔离查询',endpoint='http://127.0.0.1:9999/mcp',
        tool_name='query',auth_env='DOCQA_MCP_ISOLATED_MISSING',input_schema={'type':'object','properties':{}},
        protocol_version='2025-11-25',read_only=True,enabled=True))
    IntentManagement(env.repository).save(dict(code='isolated-query',name='隔离查询',kind='MCP',base_id=base['id'],
        tool_id='missing-credential',examples=['运行隔离查询'],enabled=True))
    q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'credential');task=q.submit(conv['id'],'运行隔离查询','ask')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id']);row=q.present(q.require_task(task['id']))
    assert row['status']=='failed' and row['error_code']=='tool_credentials_missing' and row['requests_used']==0
    assert not env.model.calls and not env.embedding.calls
