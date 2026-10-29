"""有界语义复核的协议、费用与发布边界；桩不冒充语义质量验证。"""
from copy import deepcopy
import json

import pytest

from app.answer_semantic_review import DIMENSIONS,SEMANTIC_REVIEW_VERSION,request,validate,verify_stored_publication
from app.rag_validation import ModelOutputError
from tests.test_rag_composition import pack


def answer():return {'status':'answered','answer':'甲机构核对封装。','conclusion':[{'text':'甲机构核对封装。','refs':[1],'context':[],'claim_type':'direct'}],
    'explanation':[],'citations':[],'answer_parts':[{'part_id':'p1','state':'answered','fact_indices':[0],'missing_detail':''}],
    'missing_points':[],'clarification_questions':[],'presentation':{'blocks':[{'role':'evidence','kind':'paragraph','fact_indices':[0]}]}}


def report(value,verdict='accept'):
    checks={k:'pass' for k in DIMENSIONS};findings=[]
    if verdict!='accept':
        checks['conditions']='fail' if verdict=='reject' else 'uncertain';s=value['source_items'][0]
        findings=[{'dimensions':['conditions'],'fact_indices':[0],'part_ids':[value['answer']['answer_parts'][0]['part_id']],
            'sources':[{'source_id':s['source_id'],'quote':s['ranges'][0]['text']}],'explanation':'仅作独立接口桩；核对发现条件缺口。'}]
    return {'version':SEMANTIC_REVIEW_VERSION,'input_hash':value['input_hash'],'verdict':verdict,'checks':checks,'findings':findings}


def test_request_binds_whole_candidate_and_visible_sources_without_expected_labels_or_internal_ids():
    source=pack('甲机构核对封装。未经核对不得放行。');source.items[0].read_ranges=[[0,8]]
    value=request('说明放行条件。',source,answer())
    assert value['source_items'][0]['ranges'][0]['text']=='甲机构核对封装。'
    assert all(k not in value['source_items'][0] for k in ('document_id','chunk_id','parse_version_id'))
    assert 'expected' not in value and validate(report(value),value)['verdict']=='accept'
    changed=deepcopy(answer());changed['conclusion'][0]['text']='乙机构核对封装。'
    with pytest.raises(ModelOutputError,match='semantic_review_invalid'):
        validate(report(value),request('说明放行条件。',source,changed))


@pytest.mark.parametrize('mutation',['echo','blanket_accept','unknown_dimension','hidden_quote','unknown_source','unknown_fact','unknown_part','nested_dimension','invalid_state'])
def test_invalid_or_unbound_reviewer_output_never_counts_as_acceptance(mutation):
    value=request('说明条件。',pack('甲机构核对封装。'),answer());response=report(value,'reject')
    if mutation=='echo':response['input_hash']='wrong'
    if mutation=='blanket_accept':response['verdict']='accept'
    if mutation=='unknown_dimension':response['checks']['other']='pass'
    if mutation=='hidden_quote':response['findings'][0]['sources'][0]['quote']='从未装入的另一条规定。'
    if mutation=='unknown_source':response['findings'][0]['sources'][0]['source_id']='other'
    if mutation=='unknown_fact':response['findings'][0]['fact_indices']=[99]
    if mutation=='unknown_part':response['findings'][0]['part_ids']=['p999']
    if mutation=='nested_dimension':response['findings'][0]['dimensions']=[[]]
    if mutation=='invalid_state':response['checks']['conditions']=True
    with pytest.raises(ModelOutputError,match='semantic_review_invalid') as caught:validate(response,value)
    assert caught.value.reason=='semantic_review_invalid'


def test_publication_cannot_replace_the_answer_after_an_accepted_review():
    value=request('说明条件。',pack('甲机构核对封装。'),answer());response=report(value)
    candidate={**answer(),'quality_review':response};verify_stored_publication(candidate,value,response,'说明条件。')
    candidate['conclusion'][0]['text']='甲机构可以直接放行。'
    with pytest.raises(ModelOutputError,match='semantic_review_result_mismatch'):
        verify_stored_publication(candidate,value,response,'说明条件。')


@pytest.mark.parametrize('scope',['document','knowledge'])
@pytest.mark.parametrize('verdict,status',[('accept','succeeded'),('reject','failed'),('uncertain','needs_attention'),('invalid','failed'),('timeout','needs_attention')])
def test_worker_review_is_a_separate_accounted_step_and_cannot_publish_a_rejected_or_unknown_answer(tmp_path,scope,verdict,status):
    from app.deepseek import ModelError
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    from app.product_access import ProductAccess
    from app.question_worker import QuestionWorker
    from tests.test_question_jobs import question_fixture,submit,LocalRuntime,REPLY,QUESTION
    env=question_fixture(tmp_path,question_max_requests=3,answer_semantic_review_enabled=True)
    if scope=='document':task,_=submit(env);worker=QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime())
    else:
        access=ProductAccess(env.repository,env.settings,env.rag.index);base=access.create_base('独立复核资料','scope')
        access.bind_document(base['id'],'qdoc');access.publish(base['id'],0);questions=KnowledgeQuestions(access,env.rag)
        task=questions.submit(questions.create(base['id'],'review')['id'],QUESTION,'ask')
        worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    def generate(system,user):
        value=json.loads(user)
        if value.get('version')==SEMANTIC_REVIEW_VERSION:
            if verdict=='timeout':raise ModelError(504,'isolated timeout')
            return '{}' if verdict=='invalid' else json.dumps(report(value,verdict),ensure_ascii=False)
        return REPLY
    env.model.handler=generate
    if scope=='document':
        assert task['provider_requests_expected']==3
        worker.run_job(task['id']);done=env.service.get_job(task['id'])
        calls=done['calls'];has_result=done['result_id'] is not None
        if verdict=='accept':assert env.service.get_result(done['result_id'])['answer']['quality_review']['verdict']=='accept'
    else:
        worker.run_once(task['id']);done=questions.present(questions.require_task(task['id']))
        with env.repository.connect() as db:calls=[dict(r) for r in db.execute('SELECT status FROM kb_task_calls WHERE task_id=? ORDER BY sequence_no',(task['id'],))]
        has_result=done['result'] is not None
        if verdict=='accept':assert done['result']['quality_review']['verdict']=='accept'
    assert done['status']==status,done
    assert has_result is (verdict=='accept')
    assert done['requests_used']==3 and len(env.embedding.calls)==1 and len(env.model.calls)==2
    assert [c['status'] for c in calls[:2]]==['succeeded','succeeded']
    assert calls[-1]['status']==('uncertain' if verdict=='timeout' else 'failed' if verdict=='invalid' else 'succeeded')
    if scope=='document':assert not worker.run_job(task['id'])
    else:assert not worker.run_once(task['id'])
    assert len(env.model.calls)==2


def test_review_budget_is_rejected_before_any_upstream_request(tmp_path):
    from app.rag_context import RagError
    from tests.test_question_jobs import question_fixture,submit
    env=question_fixture(tmp_path,question_max_requests=2,answer_semantic_review_enabled=True)
    with pytest.raises(RagError,match='至少3'):submit(env)
    assert not env.embedding.calls and not env.model.calls


def lifecycle(tmp_path,scope):
    from types import SimpleNamespace
    from tests.test_question_jobs import question_fixture,submit,LocalRuntime,QUESTION
    from app.question_worker import QuestionWorker
    env=question_fixture(tmp_path,question_max_requests=3,answer_semantic_review_enabled=True)
    if scope=='document':
        task,_=submit(env)
        return SimpleNamespace(env=env,task=task,worker=QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()),
            run=lambda w:w.run_job(task['id']),get=lambda:env.service.get_job(task['id']),
            retry=lambda:env.service.retry_job(task['id'],idempotency_key='explicit-retry'),cancel=lambda:env.service.cancel_job(task['id']))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.product_access import ProductAccess
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    from app.knowledge_routes import install_knowledge_routes
    access=ProductAccess(env.repository,env.settings,env.rag.index);base=access.create_base('复核生命周期','scope')
    access.bind_document(base['id'],'qdoc');access.publish(base['id'],0);questions=KnowledgeQuestions(access,env.rag)
    task=questions.submit(questions.create(base['id'],'lifecycle')['id'],QUESTION,'ask')
    app=FastAPI();install_knowledge_routes(app,access,env.rag)
    def post(action):
        with TestClient(app) as client:
            response=client.post('/api/kb-tasks/'+task['id']+'/'+action,json={'acknowledge_uncertain':False} if action=='retry' else {})
            assert response.status_code==200,response.text
            return response.json()
    return SimpleNamespace(env=env,task=task,worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model),
        run=lambda w:w.run_once(task['id']),get=lambda:questions.present(questions.require_task(task['id'])),retry=lambda:post('retry'),cancel=lambda:post('cancel'))


@pytest.mark.parametrize('scope',['document','knowledge'])
@pytest.mark.parametrize('boundary',['known_reject','stop_before_review','stop_after_review','cancel_during_writer'])
def test_review_resume_and_stop_boundaries_keep_completed_http_without_repeating_writer(tmp_path,monkeypatch,scope,boundary):
    from tests.test_question_jobs import REPLY
    flow=lifecycle(tmp_path,scope);env=flow.env;worker=flow.worker
    def generate(system,user):
        value=json.loads(user)
        if value.get('version')==SEMANTIC_REVIEW_VERSION:
            if boundary=='stop_after_review':worker.request_stop()
            return json.dumps(report(value,'reject' if boundary=='known_reject' else 'accept'),ensure_ascii=False)
        if boundary=='cancel_during_writer':flow.cancel()
        return REPLY
    env.model.handler=generate
    if boundary=='stop_before_review':
        if scope=='document':
            original=worker.store.save_checkpoint
            def save(*args,**kwargs):
                result=original(*args,**kwargs)
                if 'draft_answer_checkpoint' in kwargs.get('payload',{}):worker.request_stop()
                return result
            monkeypatch.setattr(worker.store,'save_checkpoint',save)
        else:
            original=worker.tasks.call
            def call(*args,**kwargs):
                result=original(*args,**kwargs)
                if kwargs.get('checkpoint')=='validated_protocol':worker.request_stop()
                return result
            monkeypatch.setattr(worker.tasks,'call',call)
    flow.run(worker);first=flow.get()
    expected_calls=2 if boundary in ('known_reject','stop_after_review') else 1
    assert len(env.model.calls)==expected_calls and first['requests_used']==expected_calls+1
    assert first['result_id'] is None if scope=='document' else first['result'] is None
    if boundary=='cancel_during_writer':
        assert first['status']=='cancelled'
        return
    assert first['status'] in ('failed','needs_attention')
    # retry入口复用有效正文和已确认复核；包括已用尽预算但不需再发HTTP的情况。
    flow.retry();worker.stop_requested=False;monkeypatch.undo()
    env.model.handler=lambda system,user:json.dumps(report(json.loads(user)),ensure_ascii=False)
    flow.run(worker);second=flow.get()
    assert second['status']==('failed' if boundary=='known_reject' else 'succeeded'),second
    assert len(env.embedding.calls)==1
    assert len(env.model.calls)==(expected_calls+1 if boundary=='stop_before_review' else expected_calls)
