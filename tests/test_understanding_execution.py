"""意图／对象到真实任务执行的闭环；语义向量来自可控制桩，无HTTP。"""
from dataclasses import replace
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from app.intent_management import IntentManagement
from app.knowledge_routes import IntentBody
from app.semantic_intents import SemanticIntents
from app.query_understanding import build_memory,understand
from app.conversations import Conversations
from app.unified_chat import UnifiedChat
from app.clarifications import resolve,cancel
from tests.test_knowledge_chain import library
from tests.test_question_jobs import LocalRuntime


def business(access,base):
    manager=IntentManagement(access.repository)
    row=manager.save(IntentBody(code='travel-policy',name='差旅政策',kind='KB',base_id=base['id'],examples=['差旅费用如何处理'],
        match_mode='exact').model_dump())
    return manager,row


def test_ambiguous_reference_is_persisted_and_zero_call(tmp_path):
    env,access,base=library(tmp_path);q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'new')
    task=q.submit(conv['id'],'两者有什么区别','question')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    value=q.present(q.require_task(task['id']))
    assert value['result']['clarification']['state']=='awaiting'
    assert value['requests_used']==0 and not env.embedding.calls and not env.model.calls
    assert q.submit(conv['id'],'两者有什么区别','question')['id']==task['id']
    chat=UnifiedChat(access,q,Conversations(access,env.service));cid='k:'+conv['id']
    clar=value['result']['clarification']['id']
    result=resolve(chat,cid,clar,['机制甲','机制乙'],'confirmed')
    again=resolve(chat,cid,clar,['机制甲','机制乙'],'confirmed')
    assert result['id']==again['id']
    assert not env.embedding.calls and not env.model.calls
    with pytest.raises(HTTPException):resolve(chat,cid,clar,['机制丙','机制丁'],'different')


def test_clarification_epoch_and_cancel_are_final(tmp_path):
    env,access,base=library(tmp_path);q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'new')
    task=q.submit(conv['id'],'它有什么要求','question')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model);worker.run_once(task['id'])
    chat=UnifiedChat(access,q,Conversations(access,env.service));cid='k:'+conv['id'];clar='clarify-'+task['id']
    cancel(chat,cid,clar)
    with pytest.raises(HTTPException):resolve(chat,cid,clar,['机制甲'],'confirmation')
    assert not env.embedding.calls and not env.model.calls


def test_context_controls_actual_route_and_keeps_original_question(tmp_path):
    env,access,base=library(tmp_path);manager,row=business(access,base)
    body=IntentBody(code=row['code'],name=row['name'],kind='KB',base_id=base['id'],match_mode='keywords',keywords=['差旅'],
        expected_revision=row['revision']).model_dump();manager.save(body,row['id'])
    memory=build_memory([{'turn':1,'question':'解释“差旅政策”，仅考虑今年，不包括海外'}])
    value=manager.evaluate('它有什么要求',base['id'],context=memory)
    assert value['route']['code']=='travel-policy'
    assert value['understanding']['original_question']=='它有什么要求'
    assert value['understanding']['bindings'][0]['names']==['差旅政策']
    assert memory['conditions'] and all(not c['facts_verified'] for c in memory['conditions'])
    ambiguous=understand('它有什么要求',build_memory([{'turn':1,'question':'比较“机制甲”和“机制乙”的区别'}]),env.settings)
    assert ambiguous['ambiguous_reference']


def test_calibrated_semantic_route_reuses_the_retrieval_query(tmp_path):
    env,access,base=library(tmp_path)
    settings=replace(env.settings,semantic_intent_enabled=True)
    env.settings=settings;access.settings=settings;env.rag.settings=settings;env.repository.settings=settings
    manager,row=business(access,base);index=SemanticIntents(env.repository)
    class ExampleEmbedding:
        signature=env.embedding.signature
        settings=env.embedding.settings
        calls=[]
        def embed(self,texts):self.calls.append(texts);return [[1.0,0.0] for _ in texts]
    embedding=ExampleEmbedding()
    prepared=index.prepare(manager.list(),embedding)
    assert prepared['expected_http_requests']==1
    index.prepare(manager.list(),embedding);assert len(embedding.calls)==1
    cases=[{'id':str(i),'base_id':base['id'],'document_group':'positive' if i<3 else 'negative',
        'query_vector':[1.0,0.0] if i<3 else [0.0,1.0],'expected_code':row['code'] if i<3 else 'knowledge-default'} for i in range(6)]
    index.calibrate(manager.list(),embedding.signature,cases,threshold=0.9,margin=0.1)
    assert manager.evaluate('费用制度规定',base['id'],query_vector=[1.0,0.0],provider_signature=embedding.signature)['route']['code']==row['code']
    q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'new');task=q.submit(conv['id'],'退货包装有什么要求','question')
    def reply(system,user):
        references=json.loads(user)['references']
        selected=next(r for r in references if any('退货' in segment for span in r['content'] for segment in span['segments'].values()))
        return json.dumps({'status':'answered','conclusion':[{'text':'退货需要保留包装。','claim_type':'direct','context':[],
            'evidence':[{'ref':selected['ref'],'unit':1}],'part_ids':['p1']}],'explanation':[],
            'clarification_questions':[],'missing_points':[],'answer_parts':[{'part_id':'p1','state':'answered','missing_detail':''}]},ensure_ascii=False)
    env.model.handler=reply
    worker=KnowledgeWorker(settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id'])
    resolved=q.tasks.checkpoint(task['id'],'resolved_intent')
    assert resolved and resolved['route']['code']==row['code']
    assert len(env.embedding.calls)==1 and len(env.model.calls)==1
    assert q.present(q.require_task(task['id']))['status']=='succeeded'
    with env.repository.connect() as db:db.execute('UPDATE intent_nodes SET enabled=0,revision=revision+1 WHERE id=?',(row['id'],))
    assert manager.evaluate('费用制度规定',base['id'],query_vector=[1.0,0.0],provider_signature=embedding.signature)['route']['code']=='knowledge-default'
