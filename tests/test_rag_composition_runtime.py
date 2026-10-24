"""规划、正文分别计费，明确重试只复用有效计划；使用离线供应商桩。"""
import json
import pytest
from app.deepseek import ModelError
from app.rag_context import RagError
from app.question_worker import QuestionWorker
from tests.test_question_jobs import question_fixture,submit,LocalRuntime,REPLY
from tests.test_knowledge_chain import library
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker

QUESTION='请比较章节总数与第一部分主题。'


def provider(system,user):
    if '选材与段落规划器' in system:
        value=json.loads(user);unit=value['evidence_units'][0]
        selected={'ref':unit['ref'],'unit':unit['unit']}
        return json.dumps({'blocks':[{'role':'comparison','focus':'两个信息维度','part_ids':['p1'],
            'groups':[{'label':'总数','units':[selected]},{'label':'主题','units':[selected]}]}],'missing_part_ids':[]},ensure_ascii=False)
    body=json.loads(REPLY);body['presentation']['blocks'][0]['role']='comparison'
    return json.dumps(body,ensure_ascii=False)


def test_complex_job_declares_three_http_and_persists_plan_in_existing_checkpoint(tmp_path):
    env=question_fixture(tmp_path,question_max_requests=3);env.model.handler=provider
    job,_=submit(env,question=QUESTION)
    assert job['provider_requests_expected']==3 and job['request_upper_bound']==3
    worker=QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime());worker.run_job(job['id'])
    row=env.service.get_job(job['id']);assert row['status']=='succeeded',row
    assert row['requests_used']==3 and row['steps_completed']==row['steps_total']==3
    assert [c['role'] for c in row['calls']]==['embedding','generation','generation']
    assert env.service.store.checkpoint(job['id'],'evidence')['composition_checkpoint']['plan']['blocks']
    assert 'planning' in [s['stage'] for s in row['stage_history']]


def test_insufficient_declared_budget_rejects_before_any_external_call(tmp_path):
    env=question_fixture(tmp_path,question_max_requests=2)
    with pytest.raises(RagError,match='至少3'):submit(env,question=QUESTION)
    assert not env.embedding.calls and not env.model.calls


def test_plan_validation_failure_never_starts_writer(tmp_path):
    env=question_fixture(tmp_path,question_max_requests=3)
    def wrong(system,user):
        value=json.loads(provider(system,user));value['blocks'][0]['groups'][0]['units'][0]['unit']=999
        return json.dumps(value)
    env.model.handler=wrong;job,_=submit(env,question=QUESTION)
    QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    row=env.service.get_job(job['id']);assert row['status']=='failed' and not row['result_id']
    assert row['requests_used']==2 and len(env.model.calls)==1


def test_known_writer_failure_retry_reuses_retrieval_and_plan_without_rebilling(tmp_path):
    env=question_fixture(tmp_path,question_max_requests=4)
    def fail_writer(system,user):
        if '选材与段落规划器' not in system:raise ModelError(502,'模拟已知拒绝')
        return provider(system,user)
    env.model.handler=fail_writer;job,_=submit(env,question=QUESTION)
    worker=QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime());worker.run_job(job['id'])
    assert env.service.get_job(job['id'])['status']=='failed'
    env.model.handler=provider;env.service.retry_job(job['id'],idempotency_key='explicit-retry')
    worker.run_job(job['id']);row=env.service.get_job(job['id'])
    assert row['status']=='succeeded',row
    assert row['requests_used']==4 and len(env.model.calls)==3 and len(env.embedding.calls)==1
    assert sum('选材与段落规划器' in system for system,_ in env.model.calls)==1


def kb_provider(system,user):
    value=json.loads(user)
    if '选材与段落规划器' in system:
        unit=next(u for u in value['evidence_units'] if any('21' in p['text'] for p in u['passages']))
        selected={'ref':unit['ref'],'unit':unit['unit']}
        return json.dumps({'blocks':[{'role':'comparison','focus':'总数与主题','part_ids':['p1'],
            'groups':[{'label':'总数','units':[selected]},{'label':'主题','units':[selected]}]}],'missing_part_ids':[]})
    ref=next(r['ref'] for r in value['references'] if r['document_name']=='合成问答样本.txt')
    body=json.loads(REPLY);body['presentation']['blocks'][0]['role']='comparison'
    body['conclusion'][0]['evidence'][0]['ref']=ref
    body['conclusion'][0]['context'][0]['ref']=ref
    return json.dumps(body,ensure_ascii=False)


def test_kb_worker_resume_reuses_valid_plan_and_vector_with_no_schema_change(tmp_path):
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag)
    conv=service.create(base['id'],'composition');task=service.submit(conv['id'],QUESTION,'one')
    assert task['provider_requests_expected']==3
    def fail_writer(system,user):
        if '选材与段落规划器' not in system:raise ModelError(502,'模拟已知拒绝')
        return kb_provider(system,user)
    env.model.handler=fail_writer
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id']);row=service.require_task(task['id'])
    assert row['status']=='failed' and row['requests_used']==3,row
    assert worker.tasks.checkpoint(task['id'],'composition_plan')['plan']['blocks']
    # 模拟显式重试端点对已知失败的重新排队；不抹除调用或检查点。
    with env.repository.connect() as db:db.execute("UPDATE kb_tasks SET status='queued' WHERE id=?",(task['id'],))
    env.model.handler=kb_provider;worker.run_once(task['id']);row=service.present(service.require_task(task['id']))
    assert row['status']=='succeeded',row
    assert row['requests_used']==4 and len(env.embedding.calls)==1 and len(env.model.calls)==3
    assert sum('选材与段落规划器' in s for s,_ in env.model.calls)==1
    with env.repository.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM kb_trace_spans WHERE task_id=? AND kind='COMPOSITION'",(task['id'],)).fetchone()[0]==2


def test_kb_old_task_keeps_historical_request_estimate(tmp_path):
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag)
    task=service.submit(service.create(base['id'],'historical')['id'],QUESTION,'one')
    row=service.require_task(task['id']);payload=json.loads(row['input_json'])
    payload.pop('provider_requests_expected');row['input_json']=json.dumps(payload)
    assert service.present(row)['provider_requests_expected']==2
