"""实用意图：两个会话入口、无调用系统回复、配置测试、歧义及范围控制。"""
import json
from pathlib import Path
import pytest

from app.intent_management import IntentManagement
from app.intent_replies import interaction_answer
from app.product_access import ProductAccess
from app.question_worker import QuestionWorker
from app.knowledge_traces import KnowledgeTraces
from tests.test_question_jobs import question_fixture, LocalRuntime, QUESTION
from tests.test_product_access import team
from tests.test_conversations import conversation, turn

CASES = json.loads((Path(__file__).parent/'fixtures/intents/routing_cases.json').read_text(encoding='utf8'))['cases']


@pytest.fixture(scope='module')
def rules(tmp_path_factory):
    env=question_fixture(tmp_path_factory.mktemp('routing'))
    return env,IntentManagement(env.repository)


@pytest.mark.parametrize('case',CASES,ids=[c['id'] for c in CASES])
def test_frozen_routing_corpus(rules,case):
    env,intents=rules
    for scope in ('document','knowledge'):
        assert intents.evaluate(case['question'],scope=scope)['route']['code']==case['expected']
    assert not env.embedding.calls and not env.model.calls


def node(code,kind='SYSTEM',**values):
    return dict(code=code,name=code,kind=kind,parent_id=None,description='',examples=[],
                response_text='合成回复',top_k=None,enabled=True,sort_order=10,**values)


def test_keywords_only_affect_the_selected_library_and_ambiguity_falls_back(tmp_path):
    env=question_fixture(tmp_path);access=ProductAccess(env.repository,env.settings,env.rag.index)
    a=access.create_base('甲库','');b=access.create_base('乙库','');intents=IntentManagement(env.repository)
    value=intents.save(node('refund','KB',base_id=a['id'],match_mode='keywords',keywords=['退款','退货']))
    route=intents.evaluate('谢谢，退款多久到账',a['id'])
    assert route['route']['id']==value['id'] and route['decision']['method']=='keywords'
    assert intents.resolve('退款多久到账',b['id'])['code']=='knowledge-default'
    assert intents.resolve('退款多久到账',a['id'],scope='document')['code']=='knowledge-default'
    intents.save(node('returns','KB',base_id=a['id'],match_mode='keywords',keywords=['退款']))
    ambiguous=intents.evaluate('退款多久到账',a['id'])
    assert ambiguous['route']['code']=='knowledge-default' and len(ambiguous['decision']['candidates'])==2


def test_disabled_parent_scope_and_configuration_are_effective(tmp_path):
    env=question_fixture(tmp_path);intents=IntentManagement(env.repository)
    before=intents.evaluate('你好呀',scope='document')
    parent=next(i for i in intents.list() if i['code']=='sys')
    parent.update(enabled=False,expected_revision=parent['revision'])
    intents.save(parent,parent['id'])
    after=intents.evaluate('你好呀',scope='document')
    assert after['route']['code']=='knowledge-default'
    assert after['decision']['configuration']!=before['decision']['configuration']


def test_conflicting_exact_examples_do_not_silently_choose_a_reply(tmp_path):
    env=question_fixture(tmp_path);intents=IntentManagement(env.repository)
    v=node('duplicate');v['examples']=['你好'];intents.save(v)
    result=intents.evaluate('你好')
    assert result['decision']['method']=='fallback' and len(result['decision']['candidates'])==2


def test_parent_library_scope_is_inherited(tmp_path):
    env=question_fixture(tmp_path);access=ProductAccess(env.repository,env.settings,env.rag.index)
    base=access.create_base('受限库','');intents=IntentManagement(env.repository)
    parent=intents.save(node('restricted',base_id=base['id']))
    child=node('restricted-child');child.update(parent_id=parent['id'],examples=['合成招呼'])
    intents.save(child)
    assert intents.resolve('合成招呼',None)['code']=='knowledge-default'
    assert intents.resolve('合成招呼',base['id'])['code']=='restricted-child'


def test_backend_bench_is_admin_only_zero_call_and_creates_no_tasks(team):
    env,_,clients=team;path='/api/admin/intents/test'
    payload={'questions':['你好呀','谢谢，退款多久到账'],'scope':'document'}
    assert clients['alice'].post(path,json=payload).status_code==403
    assert clients['admin'].post(path,json=payload,headers={'X-CSRF-Token':''}).status_code==403
    result=clients['admin'].post(path,json=payload)
    assert result.status_code==200,result.text
    assert [r['decision']['code'] for r in result.json()['results']]==['sys-welcome','knowledge-default']
    assert not result.json()['persisted'] and result.json()['supplier_calls']==0
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM question_jobs').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM kb_tasks').fetchone()[0]==0
    assert not env.embedding.calls and not env.model.calls


def test_document_conversation_interactions_persist_with_zero_calls_and_no_memory_pollution(team):
    env,_,clients,_,conv=conversation(team);alice=clients['alice']
    response=turn(alice,conv['id'],question='你好呀');assert response.status_code==202,response.text
    job=response.json()['job'];assert job['provider_requests_expected']==0 and job['intent']['code']=='sys-welcome'
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    done=env.service.get_job(job['id']);assert done['status']=='succeeded',done
    result=env.service.get_result(done['result_id'])['answer']
    assert result['response_kind']=='system' and not result['citations'] and not result['conclusion']
    assert done['requests_used']==0 and not env.embedding.calls and not env.model.calls
    assert turn(alice,conv['id'],question='你好呀').json()['job']['id']==job['id']
    detail=alice.get('/api/conversations/'+conv['id']).json()
    assert detail['turns'][0]['result']['answer']['answer']==result['answer']
    assert not detail['memory']['context']['recent_questions']
    exported=alice.get('/api/question-results/'+done['result_id']+'/export?format=markdown')
    assert result['answer'] in exported.text
    trace=KnowledgeTraces(env.repository).detail(job['id'])
    assert trace['intent']['code']=='sys-welcome' and '你好呀' not in json.dumps(trace,ensure_ascii=False)
    business=turn(alice,conv['id'],key='business',question=QUESTION).json()['job']
    assert business['max_requests']==2
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(business['id'])
    assert env.service.get_job(business['id'])['status']=='succeeded'
    assert len(env.embedding.calls)==1 and len(env.model.calls)==1


def test_changed_policy_stops_queued_interaction_before_any_provider_call(team):
    env,_,clients,_,conv=conversation(team)
    job=turn(clients['alice'],conv['id'],question='谢谢您').json()['job']
    intents=IntentManagement(env.repository);row=next(r for r in intents.list() if r['code']=='sys-thanks')
    row.update(enabled=False,expected_revision=row['revision']);intents.save(row,row['id'])
    QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    done=env.service.get_job(job['id'])
    assert done['status']=='failed' and done['error_code']=='intent_changed'
    assert not env.embedding.calls and not env.model.calls


def test_interaction_publisher_cannot_publish_fabricated_knowledge_or_reply(team):
    env,_,clients,_,conv=conversation(team)
    job=turn(clients['alice'],conv['id'],question='你好呀').json()['job']
    claimed=env.service.store.claim('manual',60,job_id=job['id']);snapshot=json.loads(claimed['snapshot_json'])
    answer=interaction_answer(claimed,snapshot)
    assert env.service.store.reserve(job['id'],claimed['lease_token'],'generation') is None
    with pytest.raises(ValueError):env.service.store.publish(job['id'],claimed['lease_token'],answer)
    answer['answer']='伪造的知识结论'
    with pytest.raises(ValueError):env.service.store.publish_interaction(job['id'],claimed['lease_token'],answer)
    assert env.service.get_job(job['id'])['result_id'] is None


@pytest.mark.parametrize('policy',[{'match_mode':'keywords','keywords':['退款']},
                                  {'match_mode':'interaction','interaction_kind':'thanks'}])
def test_tool_routes_cannot_use_broad_matching(tmp_path,policy):
    from fastapi import HTTPException
    env=question_fixture(tmp_path);intents=IntentManagement(env.repository)
    with pytest.raises(HTTPException):intents.save(node('bad-tool','MCP',tool_id='fake',base_id='fake',**policy))


def test_preview_cookie_does_not_replace_existing_account_on_same_host(team,tmp_path):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.test_product_access import PASSWORD
    env,_,clients=team
    preview=question_fixture(tmp_path/'preview')
    settings=replace(preview.settings,session_cookie_name='docqa_preview_test')
    access=ProductAccess(preview.repository,settings,preview.rag.index)
    access.create_user('preview','合成预览',PASSWORD,'admin')
    # HTTP验收实例遵循本机setup后的Cookie配置；不向HTTP请求发送Secure Cookie。
    with preview.repository.connect() as db:db.execute('UPDATE product_installation SET team_enabled=1,cookie_secure=0 WHERE singleton=1')
    normal=clients['alice'];prior=normal.cookies.get('docqa_session')
    with TestClient(create_app(settings),base_url='http://testserver:8998') as local:
        local.cookies.update(normal.cookies)
        result=local.post('/api/auth/login',json={'username':'preview','password':PASSWORD})
        assert result.status_code==200
        assert local.cookies.get('docqa_session')==prior
        assert local.cookies.get('docqa_preview_test')
        assert 'docqa_preview_test=' in result.headers['set-cookie']
        normal.cookies.update(local.cookies)
        assert normal.get('/api/auth/me').json()['user']['username']=='alice'
        assert local.get('/api/auth/me').json()['user']['username']=='preview'
        local.headers['X-CSRF-Token']=result.json()['csrf_token']
        assert local.post('/api/auth/logout').status_code==200
        assert local.cookies.get('docqa_session')==prior


def test_v12_migration_preserves_custom_examples_and_old_fields(tmp_path,monkeypatch):
    from app import migrations
    from app.repository import Repository
    repository=Repository(tmp_path/'docqa.db')
    with monkeypatch.context() as earlier:
        earlier.setattr(migrations,'SCHEMA_VERSION',11)
        repository.initialize()
    with repository.connect() as db:
        db.execute("UPDATE intent_nodes SET examples_json='[\"自定义问候\"]',response_text='旧管理员回复' WHERE code='sys-welcome'")
        columns=[r[1] for r in db.execute('PRAGMA table_info(intent_nodes)')]
        old=[tuple(r) for r in db.execute('SELECT '+','.join(columns)+' FROM intent_nodes ORDER BY id')]
    assert repository.initialize()['applied']==list(range(12,migrations.SCHEMA_VERSION+1))
    with repository.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT '+','.join(columns)+' FROM intent_nodes ORDER BY id')]==old
    rows={r['code']:r for r in IntentManagement(repository).list()}
    assert rows['sys-welcome']['match_mode']=='exact'
    assert rows['sys-thanks']['match_mode']=='interaction'
    assert IntentManagement(repository).evaluate('你好呀')['route']['code']=='knowledge-default'
    assert IntentManagement(repository).evaluate('自定义问候')['route']['response_text']=='旧管理员回复'
    assert repository.initialize()['applied']==[]


def test_v12_migration_failure_rolls_back_policy_columns(tmp_path,monkeypatch):
    from app import migrations
    from app.repository import Repository
    repository=Repository(tmp_path/'docqa.db')
    with monkeypatch.context() as earlier:
        earlier.setattr(migrations,'SCHEMA_VERSION',11)
        repository.initialize()
    def fail():raise RuntimeError('synthetic migration failure')
    migrations.MigrationHook.register(12,fail)
    try:
        with pytest.raises(migrations.MigrationError):repository.initialize()
    finally:migrations.MigrationHook.clear()
    with repository.connect() as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==11
        assert 'match_mode' not in [r[1] for r in db.execute('PRAGMA table_info(intent_nodes)')]
    assert repository.initialize()['applied']==list(range(12,migrations.SCHEMA_VERSION+1))
