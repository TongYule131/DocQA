"""受控验收中MCP模拟与真实SDK客户端隔离；仅使用本地MockTransport。"""
import json
from pathlib import Path
from hashlib import sha256
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.deepseek import DeepSeekModel
from scripts.close_intent_answer_evaluation import Runtime


@pytest.mark.parametrize('stream',[False,True])
def test_business_mock_keeps_sdk_client_type_and_provider_transport_separate(tmp_path,monkeypatch,stream):
    import app.deepseek as dm
    from openai import OpenAI
    inputs=json.loads(Path('tests/fixtures/intents/responsibility_inputs.json').read_text('utf-8'))
    case={'id':'IR-D01','split':'development'};sent=[];client_class=httpx.Client
    def respond(request):
        sent.append(json.loads(request.content))
        common={'id':'local-sdk-check','object':'chat.completion','created':1,'model':'local-only'}
        if not stream:return httpx.Response(200,json={**common,'choices':[{'index':0,'message':{'role':'assistant','content':'{}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
        chunks=[{**common,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':'{}'},'finish_reason':None}]},
            {**common,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]},
            {**common,'object':'chat.completion.chunk','choices':[],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}]
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
    def sdk(**kwargs):return OpenAI(**kwargs,http_client=client_class(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(dm,'OpenAI',sdk)
    settings=Settings(data_dir=tmp_path,deepseek_api_key='local-only',embedding_api_key='local-only',team_mode=True,session_cookie_secure=False)
    model=DeepSeekModel(settings)
    with Runtime(case,inputs['cases'][case['id']],inputs['corpora'][case['split']],settings,tmp_path,{},model) as runtime:
        assert httpx.Client is client_class
        if stream:
            received=[];raw=runtime.model.generate_stream('接口检查','{}',received.append,lambda:None)
            assert ''.join(received)=='{}'
        else:raw=runtime.model.generate('接口检查','{}')
        assert raw=='{}' and len(sent)==1 and runtime.business_calls==[]


def test_verified_draft_resume_uses_a_new_database_copy_and_only_calls_the_missing_review(tmp_path):
    from tests.test_answer_semantic_review import lifecycle,report
    from tests.test_question_jobs import REPLY
    from app.answer_semantic_review import SEMANTIC_REVIEW_VERSION
    from app.deepseek import ModelError
    from scripts.close_intent_answer_evaluation import ModelSpy,resume_observation
    initial=tmp_path/'initial';initial.mkdir();flow=lifecycle(initial,'knowledge');env=flow.env
    def first(system,user):
        if json.loads(user).get('version')==SEMANTIC_REVIEW_VERSION:raise ModelError(504,'isolated first failure')
        return REPLY
    env.model.handler=first;flow.run(flow.worker)
    task=flow.worker.tasks.row(flow.task['id']);assert task['status']=='needs_attention'
    with env.repository.connect() as db:row=dict(db.execute('SELECT * FROM kb_task_calls WHERE task_id=? AND sequence_no=3',(task['id'],)).fetchone())
    observed={'task':task,'id':'resume-fixture','payload':json.loads(task['input_json']),
        'model_inputs':[{'system':s,'user':u} for s,u in env.model.calls],'model_outputs':[REPLY],
        'main_calls':{'embedding':1,'generation':2,'tool_http':0}}
    observation=initial/'observed.json';observation.write_text(json.dumps(observed,ensure_ascii=False),'utf-8')
    old_hash=sha256(env.repository.path.read_bytes()).hexdigest()
    source={'observation_file':str(observation),'observation_sha256':sha256(observation.read_bytes()).hexdigest(),
        'database_file':str(env.repository.path),'database_sha256':old_hash,'not_sent_local_sequence':3,'not_sent_local_row':row}
    directory=tmp_path/'resumed';directory.mkdir();env.embedding.calls.clear()
    env.embedding.handler=lambda *_:pytest.fail('saved query must be reused')
    class ReviewOnly:
        def generate(self,system,user):
            value=json.loads(user);assert value['version']==SEMANTIC_REVIEW_VERSION
            return json.dumps(report(value),ensure_ascii=False)
    runtime=SimpleNamespace(case={'id':'resume-fixture'},phase='case',directory=directory,settings=replace(env.settings,data_dir=directory),embedding=env.embedding)
    runtime.model=ModelSpy(runtime,{'scenario':'normal'},ReviewOnly())
    result=resume_observation(runtime,source)
    assert result['task']['status']=='succeeded' and result['task']['requests_used']==3
    assert result['resume_evidence']['new_provider_calls']==1 and not env.embedding.calls
    assert result['calls'][2]['delivery_status']=='not_sent' and len(result['calls'])==4
    assert sha256(env.repository.path.read_bytes()).hexdigest()==old_hash


def test_explicit_invalid_plan_retry_reuses_query_and_keeps_the_first_charged_failure(tmp_path):
    from tests.test_knowledge_chain import library
    from tests.test_rag_composition_runtime import QUESTION,kb_provider
    from tests.test_question_jobs import LocalRuntime
    from tests.test_answer_semantic_review import report
    from app.answer_semantic_review import SEMANTIC_REVIEW_VERSION
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    from app.product_access import ProductAccess
    from scripts.close_intent_answer_evaluation import ModelSpy,resume_observation
    initial=tmp_path/'initial';initial.mkdir();env,access,base=library(initial)
    settings=replace(env.settings,answer_semantic_review_enabled=True)
    env.settings=settings;env.rag.settings=settings;access=ProductAccess(env.repository,settings,env.rag.index)
    service=KnowledgeQuestions(access,env.rag);task=service.submit(service.create(base['id'],'retry')['id'],QUESTION,'one')
    wrong=None
    def invalid(system,user):
        nonlocal wrong
        wrong=json.loads(kb_provider(system,user));wrong['missing_part_ids']=['p1']
        return json.dumps(wrong,ensure_ascii=False)
    env.model.handler=invalid;worker=KnowledgeWorker(settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id']);row=worker.tasks.row(task['id']);assert row['error_code']=='output_composition_coverage_invalid'
    with env.repository.connect() as db:failed=dict(db.execute('SELECT * FROM kb_task_calls WHERE task_id=? AND sequence_no=2',(task['id'],)).fetchone())
    observation=initial/'observed.json';observation.write_text(json.dumps({'task':row,'payload':json.loads(row['input_json']),
        'model_inputs':[{'system':s,'user':u} for s,u in env.model.calls],'model_outputs':[json.dumps(wrong,ensure_ascii=False)],
        'main_calls':{'embedding':1,'generation':1,'tool_http':0}},ensure_ascii=False),'utf-8')
    old_hash=sha256(env.repository.path.read_bytes()).hexdigest()
    source={'kind':'known_invalid_planner_explicit_retry','observation_file':str(observation),'observation_sha256':sha256(observation.read_bytes()).hexdigest(),
        'database_file':str(env.repository.path),'database_sha256':old_hash,'failed_local_sequence':2,'failed_local_row':failed}
    class Valid:
        def generate(self,system,user):
            value=json.loads(user)
            return json.dumps(report(value),ensure_ascii=False) if value.get('version')==SEMANTIC_REVIEW_VERSION else kb_provider(system,user)
        def generate_stream(self,system,user,on_text,check_active):
            check_active();raw=self.generate(system,user);on_text(raw);return raw
    directory=tmp_path/'resumed';directory.mkdir();env.embedding.calls.clear();env.embedding.handler=lambda *_:pytest.fail('query must not repeat')
    runtime=SimpleNamespace(case={'id':'retry-fixture'},phase='case',directory=directory,settings=replace(settings,data_dir=directory),embedding=env.embedding)
    runtime.model=ModelSpy(runtime,{'scenario':'normal'},Valid())
    result=resume_observation(runtime,source)
    assert result['task']['status']=='succeeded' and result['task']['requests_used']==5
    assert result['main_calls']['generation']==4 and result['resume_evidence']['new_provider_calls']==3
    assert result['calls'][1]==failed and not env.embedding.calls
    assert sha256(env.repository.path.read_bytes()).hexdigest()==old_hash


def test_a_retained_real_failure_cannot_be_hidden_by_a_completed_evaluation(tmp_path,monkeypatch):
    from app.runtime import source_identity
    from scripts.close_intent_answer_evaluation import run
    import scripts.evaluate_intent_responsibilities as execution
    # 临时项目仍遵守 data 子目录约束，不能依赖 pytest 的 basetemp 恰在真实项目内。
    monkeypatch.setattr(execution,'ROOT',tmp_path)
    def write(path,value):path.write_text(json.dumps(value),'utf-8')
    authorization=tmp_path/'authorization.json';write(authorization,{'max_model_http':80})
    previous=tmp_path/'first-failure.json';write(previous,{'source_identity':source_identity(),'cases':[{'id':'retained','engineering_passed':False}]})
    plan=tmp_path/'plan.json';write(plan,{'application_source_identity':source_identity(),'authorization_sha256':sha256(authorization.read_bytes()).hexdigest(),
        'runner_sources':{},'n01_labels':{'cases':[{'id':'retained'}]},'n01_inputs':{'cases':{'retained':{}}},'n02_labels':{'cases':[]},
        'retained_failed_cases':{'retained':{'report_file':str(previous),'sha256':sha256(previous.read_bytes()).hexdigest()}}})
    with pytest.raises(ValueError,match='output_must_be_new_child_of_project_data'):
        run(plan,tmp_path/'outside-data')
    result=run(plan,tmp_path/'data'/'result')
    assert result['passed'] is False and result['passed_cases']==0 and result['retained_failed_cases']==['retained']
    assert json.loads(previous.read_text())['cases'][0]['engineering_passed'] is False


@pytest.mark.parametrize('transport_unknown',[False,True])
def test_independent_negative_is_not_lost_after_known_positive_failure_but_unknown_transport_stops(tmp_path,monkeypatch,transport_unknown):
    import scripts.close_intent_answer_evaluation as evaluation
    from tests.test_answer_semantic_review import answer,report
    from tests.test_rag_composition import pack
    from app.deepseek import ModelError
    from app.rag_validation import ModelOutputError
    from scripts.approved_live_calls import CALL_PURPOSE
    case={'id':'independent','question':'说明条件。'};called=[]
    plan={'independent_n02_cases':True,'n02_positives':{'independent':{'observation':{'model_inputs':[['system','{}']],
        'result':{'status':'answered'}}}},'n02_negatives':{'independent':{'response':{'negative':True},'plan':None}},
        'http_slots':{'N02/independent/negative_review':1}}
    monkeypatch.setattr(evaluation,'restore_writer_pack',lambda *_:(pack('甲机构核对封装。'),{}))
    def validate(value,*args):
        if not value.get('negative'):raise ModelOutputError('coverage_mismatch')
        return answer()
    monkeypatch.setattr(evaluation,'candidate_from_protocol',validate)
    class Model:
        def generate(self,system,user):
            called.append(CALL_PURPOSE.get()[0])
            if len(called)==1:
                if transport_unknown:raise ModelError(504,'isolated unknown')
                return '{}'
            return json.dumps(report(json.loads(user),'reject'),ensure_ascii=False)
    if transport_unknown:
        with pytest.raises(ModelError):evaluation.evaluate_n02_pair(plan,case,tmp_path,True,Model(),SimpleNamespace(stopped=True))
        assert called==['N02/independent/writer']
    else:
        result=evaluation.evaluate_n02_pair(plan,case,tmp_path,True,Model(),SimpleNamespace(stopped=False))
        assert not result['engineering_passed'] and result['positive_error']=='coverage_mismatch'
        assert result['negative_semantic_verdict']=='reject'
        assert called==['N02/independent/writer','N02/independent/negative_review']
