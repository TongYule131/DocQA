"""补齐语义准备、澄清取消、已发送中止和后台统计的执行边界。"""
from dataclasses import replace
import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.knowledge_tasks import KnowledgeTasks,TaskStopped
from app.knowledge_worker import KnowledgeWorker
from app.knowledge_questions import KnowledgeQuestions
from app.intent_management import IntentManagement
from app.semantic_intents import SemanticIntents
from app.resource_governance import provider_guard,mark_provider_sent
from app.query_understanding import build_memory
from app.operations_metrics import project,backup_status
from app.repository import now_iso
from tests.test_knowledge_chain import library
from tests.test_question_jobs import LocalRuntime
from tests.test_understanding_execution import business
from tests.test_product_access import team,publish


def test_sent_cancel_is_uncertain_without_poisoning_provider(tmp_path):
    env,access,base=library(tmp_path)
    class Error(Exception):
        def __init__(self,code,message):self.status_code=code
    with pytest.raises(TaskStopped) as caught:
        with provider_guard(env.settings,'deepseek',Error,delivery_tracked=True):
            mark_provider_sent();raise TaskStopped('cancelled','停止','cancelled')
    assert caught.value.uncertain
    with env.repository.connect() as db:
        row=db.execute('SELECT status,delivery_status FROM provider_guard_calls').fetchone()
        assert tuple(row)==('uncertain','uncertain')
        assert db.execute('SELECT failures FROM provider_circuits').fetchone()[0]==0


def test_semantic_preparation_runs_in_worker_and_reuses_batch_checkpoint(tmp_path):
    env,access,base=library(tmp_path);manager,node=business(access,base)
    semantics=SemanticIntents(env.repository);planned=semantics.plan(manager.list(),env.embedding)
    from app.knowledge_tasks import configuration
    payload={'operation':'intent_examples','configuration':configuration(env.settings,env.embedding),
        'semantic_configuration':planned['semantic_configuration'],'semantic_work':planned['work']}
    tasks=KnowledgeTasks(env.repository);one=tasks.create('index',payload,'semantic/one',max_requests=1)
    two=tasks.create('index',payload,'semantic/two',max_requests=1)
    assert one['id']==two['id'] and not env.embedding.calls
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(one['id']);assert tasks.row(one['id'])['status']=='succeeded'
    assert len(env.embedding.calls)==1 and tasks.row(one['id'])['requests_used']==1
    assert semantics.plan(manager.list(),env.embedding)['expected_requests']==0


def test_correction_invalidates_only_conditions_of_replaced_object():
    memory=build_memory([{'turn':1,'question':'“甲机制”仅用于甲范围'},
        {'turn':2,'question':'更正，应该是“乙机制”'}])
    assert all('甲机制' not in c['text'] for c in memory['conditions'])
    assert memory['entities'][-1]['name']=='乙机制'


def test_cancel_resolving_after_backpressure_prevents_submission(tmp_path):
    from app.unified_chat import UnifiedChat
    from app.conversations import Conversations
    from app.clarifications import resolve,cancel
    env,access,base=library(tmp_path);questions=KnowledgeQuestions(access,env.rag);conv=questions.create(base['id'],'new')
    task=questions.submit(conv['id'],'它是什么','q');worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model);worker.run_once(task['id'])
    chat=UnifiedChat(access,questions,Conversations(access,env.service));identifier='k:'+conv['id'];clar='clarify-'+task['id']
    with env.repository.connect() as db:db.execute("UPDATE conversation_clarifications SET state='resolving',resolve_key='retry',resolve_hash='old' WHERE id=?",(clar,))
    assert cancel(chat,identifier,clar)['cancelled']
    with pytest.raises(HTTPException):resolve(chat,identifier,clar,['机制甲'],'retry')
    with env.repository.connect() as db:assert db.execute('SELECT COUNT(*) FROM chat_submissions').fetchone()[0]==0


def test_operations_metrics_report_unknown_instead_of_false_zero(tmp_path):
    env,access,base=library(tmp_path)
    with env.repository.connect() as db:metrics=project(db,now_iso()[:10])
    assert metrics['total']=={'count':0,'p50_ms':None,'p95_ms':None}
    assert metrics['first_validated_block']['p95_ms'] is None and backup_status(tmp_path)['status']=='unknown'


def test_vector_cache_has_actual_memory_bound_and_immutable_values():
    from app.index_cache import VectorCache
    cache=VectorCache();values,hit=cache.decode(('doc','index'),[('a','[1,0]')],2,10000)
    assert not hit and cache.size>0
    with pytest.raises(TypeError):values['a']=(0,1)
    assert cache.decode(('doc','index'),[('a','[1,0]')],2,10000)[1]
    cache.decode(('doc','index'),[('a','[1,0]')],2,1)
    assert cache.size==0


def test_admin_prepares_async_and_customer_cannot_create_provider_work(team):
    env,users,clients=team;admin=clients['admin'];base=publish(clients,users)
    created=admin.post('/api/admin/intents',json={'code':'return-policy','name':'退货政策','kind':'KB','base_id':base['id'],'examples':['退货需要什么条件']})
    assert created.status_code==201,created.text
    body={'request_key':'prepare-once','max_requests':1}
    assert clients['alice'].post('/api/admin/intents/semantic/prepare',json=body).status_code==403
    one=admin.post('/api/admin/intents/semantic/prepare',json=body);assert one.status_code==200,one.text
    two=admin.post('/api/admin/intents/semantic/prepare',json=body);assert two.status_code==200,two.text
    assert one.json()['task_id']==two.json()['task_id'] and one.json()['provider_calls']==0
    assert one.json()['expected_http_requests']==1
    with env.repository.connect() as db:assert db.execute('SELECT COUNT(*) FROM provider_guard_calls').fetchone()[0]==0
    assert admin.post('/api/admin/intents/test',json={'questions':['它是什么'],'context':{'entities':'invalid'}}).status_code==422


def test_operations_endpoint_is_admin_metadata_with_honest_unknowns(team):
    env,users,clients=team
    assert clients['alice'].get('/api/admin/operations').status_code==403
    result=clients['admin'].get('/api/admin/operations');assert result.status_code==200,result.text
    value=result.json();assert value['usage']['verified_cost'] is None
    assert value['latency']['total']['p95_ms'] is None and value['last_backup']['status']=='unknown'
    assert value['alert_delivery']=='in_app_only'
    assert 'question' not in value and 'answer' not in value


def test_lost_clarification_confirmation_survives_refresh_with_original_key(tmp_path,monkeypatch):
    from app.unified_chat import UnifiedChat
    from app.conversations import Conversations
    from app.clarifications import resolve
    env,access,base=library(tmp_path);q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'lost')
    task=q.submit(conv['id'],'两者的区别','q');worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model);worker.run_once(task['id'])
    chat=UnifiedChat(access,q,Conversations(access,env.service));original_submit=chat.submit
    def backpressure(*args,**kwargs):raise HTTPException(429,'synthetic queue pressure')
    monkeypatch.setattr(chat,'submit',backpressure)
    with pytest.raises(HTTPException):resolve(chat,'k:'+conv['id'],'clarify-'+task['id'],[' 甲机制 ','乙机制'],'original-key')
    clarification=q.present(q.require_task(task['id']))['result']['clarification']
    assert clarification['state']=='resolving' and clarification['request_key']=='original-key'
    assert clarification['confirmed_names']==['甲机制','乙机制']
    monkeypatch.setattr(chat,'submit',original_submit)
    done=resolve(chat,'k:'+conv['id'],clarification['id'],clarification['confirmed_names'],clarification['request_key'])
    assert done['id'] and not env.embedding.calls and not env.model.calls
