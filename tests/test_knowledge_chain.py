"""完整知识库链路的离线验证，模型与向量为合成桩。"""
import json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from app.product_access import ProductAccess
from app.knowledge_pipeline import KnowledgePipeline
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from app.intent_management import IntentManagement
from app.knowledge_traces import KnowledgeTraces
from app.schemas import Document,Chunk,SourceLocation
from app.repository import now_iso
from tests.test_question_jobs import question_fixture,LocalRuntime


def library(tmp_path):
    env=question_fixture(tmp_path)
    access=ProductAccess(env.repository,env.settings,env.index if hasattr(env,'index') else env.rag.index)
    base=access.create_base('合成知识库','链路验收',collection_name='testkb')
    access.bind_document(base['id'],'qdoc')
    document=Document(id='policy',filename='合成政策.txt',size=50,created_at=now_iso(),status='uploaded',format='txt')
    env.repository.create(document)
    env.repository.finish_parse('policy',1,[Chunk(id='pc1',document_id='policy',text='退货需要保留包装。',page=1,
        parse_version_id='pv1',chunk_type='text',order_index=0,sources=[SourceLocation(format='txt',line_start=1,line_end=1)])])
    env.rag.index.build('policy')
    access.bind_document(base['id'],'policy');access.publish(base['id'],0)
    env.embedding.calls.clear();env.model.calls.clear()
    return env,access,base


def test_library_task_retrieves_multiple_documents_with_one_query_and_one_generation(tmp_path):
    env,access,base=library(tmp_path)
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'create')
    def reply(system,user):
        prompt=json.loads(user);refs=prompt['references'];facts=[]
        for entry in refs:
            text=''.join(s for span in entry['content'] for s in span['segments'].values())
            if '退货' in text:
                facts.append({'text':'退货需要保留包装。','claim_type':'direct','context':[], 'evidence':[{'ref':entry['ref'],'unit':1}],'part_ids':['p1']})
        return json.dumps({'status':'answered','conclusion':facts,'explanation':[],'clarification_questions':[],
            'missing_points':[],'answer_parts':[{'part_id':'p1','state':'answered','missing_detail':''}]},ensure_ascii=False)
    env.model.handler=reply
    task=service.submit(conv['id'],'退货有什么要求','turn1')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    task=service.present(service.require_task(task['id']))
    assert task['status']=='succeeded',task
    assert len(env.embedding.calls)==1 and len(env.model.calls)==1
    assert task['requests_used']==2
    assert {c['document_id'] for c in task['result']['citations']}=={'policy'}
    prompt=json.loads(env.model.calls[0][1])
    assert {entry['document_name'] for entry in prompt['references']}=={'合成问答样本.txt','合成政策.txt'}
    assert task['result']['answer'].endswith('[1]')
    assert service.submit(conv['id'],'退货有什么要求','turn1')['id']==task['id']
    trace=KnowledgeTraces(env.repository).detail(task['id'])
    assert len(trace['spans'])>=5
    assert all(s['elapsed_ms'] is not None for s in trace['spans'])
    assert '退货有什么要求' not in json.dumps(trace,ensure_ascii=False)


def test_index_submission_is_zero_call_and_worker_builds_with_per_batch_ledger(tmp_path):
    env,access,base=library(tmp_path);pipeline=KnowledgePipeline(access)
    task=pipeline.submit_index('policy','index',rebuild=True)
    assert env.embedding.calls==[] and env.model.calls==[]
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    row=pipeline.tasks.row(task['id']);assert row['status']=='succeeded',row
    assert row['requests_used']==1 and len(env.embedding.calls)==1
    assert pipeline.submit_index('policy','index',rebuild=True)['id']==task['id']


def test_intent_tree_changes_are_immediate_and_mixed_query_keeps_knowledge_route(tmp_path):
    env,access,base=library(tmp_path);intents=IntentManagement(env.repository)
    assert intents.resolve('谢谢！',base['id'])['kind']=='SYSTEM'
    assert intents.resolve('谢谢，退货需要什么条件？',base['id'])['kind']=='KB'
    node=next(r for r in intents.list() if r['code']=='sys-thanks')
    node['enabled']=False;node['expected_revision']=node['revision']
    intents.save({k:v for k,v in node.items() if k not in ['id','level','path','created_at','updated_at','revision']},node['id'])
    assert intents.resolve('谢谢',base['id'])['kind']=='KB'
    node['parent_id']=node['id']
    with pytest.raises(Exception):intents.save(node,node['id'])


def test_greeting_uses_no_model_or_vector_and_keeps_real_trace(tmp_path):
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag)
    conv=service.create(base['id'],'greeting');task=service.submit(conv['id'],'你好','turn')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    result=service.present(service.require_task(task['id']))
    assert result['status']=='succeeded' and result['result']['route']=='sys-welcome'
    assert result['requests_used']==0 and not env.embedding.calls and not env.model.calls


def test_chunk_boundary_revision_preserves_old_version_and_rejects_omissions(tmp_path):
    env,access,_=library(tmp_path);pipeline=KnowledgePipeline(access)
    old=env.repository.get('policy').active_parse_version_id
    original=env.repository.chunks('policy',old)[0]
    with pytest.raises(Exception):pipeline.revise('policy',old,[dict(chunk_ids=[original.id],start=0,end=2)],'删除条件')
    edits=[dict(chunk_ids=[original.id],start=0,end=4),dict(chunk_ids=[original.id],start=4,end=len(original.text))]
    result=pipeline.revise('policy',old,edits,'调整分块边界')
    assert result['version']['id']!=old
    assert ''.join(c['text'] for c in result['chunks'])==original.text
    assert env.repository.chunks('policy',old)[0].text==original.text
    assert env.rag.index.index_info('policy')['parse_version_id']==old


def test_markdown_local_parse_preserves_code_fences_and_heading_sources(tmp_path):
    from app.markdown_document import markdown_spans
    from app.file_detect import detect_format
    text='# 售后说明\n\n退货需要原包装。\n\n```python\n# 不应成为标题\nprint(1)\n```\n\n## 物流\n签收后处理。'
    spans=markdown_spans(text)
    assert detect_format('policy.md',text.encode()).format=='md'
    assert sum(label=='section_header' for _,_,label in spans)==2
    assert any('# 不应成为标题' in text[a:b] and label=='text' for a,b,label in spans)
