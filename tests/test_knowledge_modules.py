"""知识库生产路径的隔离、恢复与真实协议模拟；不访问收费服务。"""
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.test_knowledge_chain import library
from tests.test_product_access import team, publish
from tests.test_question_jobs import LocalRuntime, Embedding, Model, QUESTION
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from app.knowledge_tasks import KnowledgeTasks, dump
from app.knowledge_tools import ToolRegistry, validate_schema, validate_arguments
from app.intent_management import IntentManagement
from app.main import create_app


def worker(env):return KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)


def test_atomic_upload_binds_once_and_markdown_is_parsed_locally(team):
    env,_,clients=team;admin=clients['admin']
    base=admin.post('/api/knowledge-bases',json={'name':'Markdown资料','collection_name':'markdownkb','chunk_max_chars':400,'chunk_overlap_chars':40}).json()
    route=f'/api/admin/knowledge/bases/{base["id"]}/documents'
    original='# 售后说明\n\n退货需要原包装。\n\n## 运费\n质量问题由商家承担。\n'
    def upload(text):return admin.post(route,files={'file':('policy.md',text.encode(),'text/markdown')},headers={'Idempotency-Key':'md-upload'})
    response=upload(original);assert response.status_code==201,response.text
    identifier=response.json()['document']['id']
    assert upload(original).json()['document']['id']==identifier
    assert upload(original+'修改').status_code==409
    assert admin.post(route,files={'file':('policy.md',original.encode())},headers={'Idempotency-Key':''}).status_code==422
    assert clients['alice'].post(route,files={'file':('policy.md',original.encode())},headers={'Idempotency-Key':'denied'}).status_code==403
    docs=admin.get(f'/api/knowledge-bases/{base["id"]}').json()['documents']
    assert [d['id'] for d in docs]==[identifier]
    assert admin.post(f'/api/documents/{identifier}/parse',json={'idempotency_key':'md-parse'}).status_code==202
    from app.parse_worker import ParseWorker
    parser=ParseWorker(env.settings,env.repository);assert parser.run_forever(max_tasks=1)==1
    document=env.repository.get(identifier)
    assert document.status=='parsed',document
    chunks=env.repository.chunks(identifier,document.active_parse_version_id)
    assert any('退货需要原包装' in c.text for c in chunks)
    assert all(s.format=='md' and s.line_start for c in chunks for s in c.sources)
    assert not env.embedding.calls and not env.model.calls


def test_customer_scope_and_admin_endpoints_and_page_auth(team):
    env,users,clients=team;base=publish(clients,users)
    alice,bob,admin=clients['alice'],clients['bob'],clients['admin']
    conv=alice.post('/api/kb-conversations',json={'base_id':base['id'],'request_key':'create'}).json()
    task=alice.post(f'/api/kb-conversations/{conv["id"]}/turns',json={'question':'你好','request_key':'turn'}).json()
    assert bob.get(f'/api/kb-conversations/{conv["id"]}').status_code==404
    assert admin.get(f'/api/kb-tasks/{task["id"]}').status_code==404
    for path in ['/api/admin/intents','/api/admin/traces','/api/admin/mcp-tools','/api/admin/knowledge/documents/qdoc/chunks']:
        assert alice.get(path).status_code==403
    assert clients['anonymous'].get('/app/knowledge',follow_redirects=False).status_code==303
    admin.put(f'/api/knowledge-bases/{base["id"]}/members/{users["alice"]["id"]}',json={'enabled':False})
    assert alice.get(f'/api/kb-tasks/{task["id"]}').status_code==404
    assert worker(env).run_once(task['id']) is False
    assert not env.model.calls and not env.embedding.calls


def test_memory_clear_preserves_history_but_does_not_reintroduce_old_context(tmp_path):
    env,access,base=library(tmp_path);questions=KnowledgeQuestions(access,env.rag)
    conv=questions.create(base['id'],'c');task=questions.submit(conv['id'],'你好','one');worker(env).run_once(task['id'])
    assert questions.detail(conv['id'])['turns'][0]['request_key']==conv['id']+'/one'
    assert questions.memory(conv['id'],False,True)['turns'][0]['result']
    questions.memory(conv['id'],True,False)
    new=questions.submit(conv['id'],QUESTION,'two')
    assert json.loads(questions.tasks.row(new['id'])['input_json'])['recent_questions']==[]


def test_generation_checkpoint_recovers_without_another_request(tmp_path,monkeypatch):
    env,access,base=library(tmp_path);q=KnowledgeQuestions(access,env.rag)
    conv=q.create(base['id'],'c');task=q.submit(conv['id'],QUESTION,'t')
    from tests.test_question_jobs import REPLY
    def reply(_system,user):
        ref=next(r['ref'] for r in json.loads(user)['references'] if r['document_name']=='合成问答样本.txt')
        body=json.loads(REPLY);body['conclusion'][0]['evidence'][0]['ref']=ref;body['conclusion'][0]['context'][0]['ref']=ref
        return dump(body)
    env.model.handler=reply
    w=worker(env);original=w.tasks.finish
    def fail_save(job,status,**kw):
        if status=='succeeded':raise ValueError('synthetic_disk_failure')
        return original(job,status,**kw)
    monkeypatch.setattr(w.tasks,'finish',fail_save);w.run_once(task['id'])
    row=w.tasks.row(task['id']);assert row['status']=='failed'
    assert w.tasks.checkpoint(task['id'],'validated_protocol')
    before=(len(env.model.calls),len(env.embedding.calls))
    with env.repository.connect() as db:db.execute("UPDATE kb_tasks SET status='queued' WHERE id=?",(task['id'],))
    worker(env).run_once(task['id'])
    assert q.tasks.row(task['id'])['status']=='succeeded'
    assert (len(env.model.calls),len(env.embedding.calls))==before==(1,1)


def test_claim_is_atomic_and_cancel_during_index_prevents_activation(tmp_path):
    env,access,_=library(tmp_path)
    from app.knowledge_pipeline import KnowledgePipeline
    pipeline=KnowledgePipeline(access);task=pipeline.submit_index('policy','index',rebuild=True)
    with ThreadPoolExecutor(max_workers=2) as pool:claimed=list(pool.map(lambda n:pipeline.tasks.claim(str(n),60,task['id']),range(2)))
    assert sum(r is not None for r in claimed)==1
    with env.repository.connect() as db:db.execute("UPDATE kb_tasks SET status='queued',lease_token=NULL,lease_expires_at=NULL WHERE id=?",(task['id'],))
    old=env.repository.get('policy').active_index_id
    def embed(texts):
        with env.repository.connect() as db:db.execute('UPDATE kb_tasks SET cancel_requested=1 WHERE id=?',(task['id'],))
        return [[1.,0.] for _ in texts]
    env.embedding.handler=embed;worker(env).run_once(task['id'])
    assert pipeline.tasks.row(task['id'])['status']!='succeeded'
    assert env.repository.get('policy').active_index_id==old
    assert pipeline.tasks.row(task['id'])['requests_used']==1


def test_expired_inflight_call_becomes_uncertain_without_automatic_retry(tmp_path):
    env,access,base=library(tmp_path);q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'c')
    task=q.submit(conv['id'],QUESTION,'t');store=q.tasks;row=store.claim('crashed',60,task['id'])
    with env.repository.connect() as db:
        db.execute("UPDATE kb_tasks SET requests_used=1,lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(task['id'],))
        db.execute("INSERT INTO kb_task_calls(id,task_id,sequence_no,role,status,started_at) VALUES ('uncertain-call',?,1,'query_embedding','intent','2000-01-01T00:00:00+00:00')",(task['id'],))
    assert worker(env).run_once(task['id']) is False
    assert store.row(task['id'])['status']=='needs_attention'
    assert not env.embedding.calls and not env.model.calls


def test_mcp_adapter_uses_real_http_protocol_and_accounts_every_request(tmp_path,monkeypatch):
    env,access,base=library(tmp_path)
    ToolRegistry(env.repository).save(dict(id='inventory',name='合成库存服务',endpoint='http://127.0.0.1:9999/mcp',tool_name='stock',auth_env=None,input_schema={'type':'object','properties':{'product':{'type':'string','minLength':1}},'required':['product']},protocol_version='2025-11-25',read_only=True,enabled=True))
    IntentManagement(env.repository).save(dict(code='inventory-query',name='库存查询',kind='MCP',base_id=base['id'],tool_id='inventory',examples=['产品A还有库存吗'],enabled=True))
    calls=[]
    def http(request):
        body=json.loads(request.content);calls.append(body)
        if body['method']=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'offline-fixture','version':'1'}}
        elif body['method']=='notifications/initialized':return httpx.Response(202)
        else:
            assert body['params']['arguments']=={'product':'产品A'}
            result={'content':[{'type':'text','text':'合成库存：有货（仅测试）。'}]}
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result},headers={'Mcp-Session-Id':'offline-session'})
    client=httpx.Client
    monkeypatch.setattr('app.knowledge_tools.httpx.Client',lambda **kw:client(transport=httpx.MockTransport(http),**kw))
    env.model.handler=lambda *_:'{"product":"产品A"}'
    q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'c');task=q.submit(conv['id'],'产品A还有库存吗','t');worker(env).run_once(task['id'])
    row=q.present(q.require_task(task['id']));assert row['status']=='succeeded',row
    assert row['requests_used']==4 and len(calls)==3 and len(env.model.calls)==1 and not env.embedding.calls
    assert row['result']['tool_source']['id']=='inventory' and not row['result']['citations']
    assert [r['method'] for r in calls]==['initialize','notifications/initialized','tools/call']


def test_schema_rejects_unsupported_or_unchecked_constraints():
    for schema in [{'type':'string','pattern':'x'},{'type':'object','required':'name'},{'type':'string','default':'long','maxLength':2}]:
        with pytest.raises(HTTPException):validate_schema(schema)
    with pytest.raises(ValueError):validate_arguments('long',{'type':'string','maxLength':2})


def test_multidocument_answer_binds_each_claim_to_its_own_source(tmp_path):
    env,access,base=library(tmp_path)
    from app.schemas import Document,Chunk,SourceLocation
    from app.repository import now_iso
    env.repository.create(Document(id='extra',filename='合成补充.txt',size=100,created_at=now_iso(),status='uploaded',format='txt'))
    env.repository.finish_parse('extra',1,[Chunk(id='extra1',document_id='extra',text='退货需要提供购买凭证。',page=1,parse_version_id='ev1',sources=[SourceLocation(format='txt',line_start=1,line_end=1)])])
    env.rag.index.build('extra');access.bind_document(base['id'],'extra');access.publish(base['id'],1);env.embedding.calls.clear()
    def reply(system,user):
        facts=[]
        for entry in json.loads(user)['references']:
            text=''.join(s for span in entry['content'] for s in span['segments'].values())
            for quote in ['退货需要保留包装。','退货需要提供购买凭证。']:
                if quote in text:facts.append(dict(text=quote,claim_type='direct',context=[],evidence=[{'ref':entry['ref'],'unit':1}],part_ids=['p1']))
        return dump(dict(status='answered',conclusion=facts,explanation=[],clarification_questions=[],missing_points=[],answer_parts=[dict(part_id='p1',state='answered',missing_detail='')]))
    env.model.handler=reply;q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'c');task=q.submit(conv['id'],'退货需要哪些材料','t');worker(env).run_once(task['id'])
    row=q.present(q.require_task(task['id']));assert row['status']=='succeeded',row
    assert {c['document_id'] for c in row['result']['citations']}=={'policy','extra'}
    assert len(row['result']['conclusion'])==2 and row['requests_used']==2
