"""P2隔离计量边界；SDK使用MockTransport，不连接真实模型。"""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3

import httpx
import pytest

from app.config import Settings
from app.context_budget import (BudgetError,COUNTERS,binding,dump,fingerprint,generation_request,
    policy,profile_scope,status,task_scope)
from app.deepseek import DeepSeekModel,ModelError
from app.repository import Repository
from app.resource_governance import owner_scope,provider_guard,mark_provider_sent,mark_provider_completed


def environment(tmp_path,**rules):
    settings=Settings(data_dir=tmp_path,deepseek_api_key='offline-only',deepseek_max_tokens=512,
                      context_budget_policy=json.dumps(rules))
    repository=Repository(tmp_path/'docqa.db');repository.initialize()
    return settings,repository


def sdk(monkeypatch,usage=None,handler=None):
    from openai import OpenAI
    requests=[]
    def respond(request):
        payload=json.loads(request.content);requests.append(payload)
        if handler:return handler(request,payload)
        return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,'model':payload['model'],
            'choices':[{'index':0,'message':{'role':'assistant','content':'合成回复'},'finish_reason':'stop'}],
            **({'usage':usage} if usage is not None else {})})
    monkeypatch.setattr('app.deepseek.OpenAI',lambda **kw:OpenAI(http_client=httpx.Client(transport=httpx.MockTransport(respond)),**kw))
    return requests


def rows(repository):
    with repository.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM context_reservations ORDER BY rowid')]


@pytest.mark.parametrize('call_style',['positional','keyword','mixed'])
def test_actual_serialized_messages_and_stage_output_are_bound_to_one_sdk_request(tmp_path,monkeypatch,call_style):
    settings,repo=environment(tmp_path,outputs={'general_dispatch':128})
    sent=sdk(monkeypatch,{'prompt_tokens':40,'completion_tokens':10,'total_tokens':50})
    with task_scope('knowledge','fixture','general_dispatch'),owner_scope('alice'):
        model=DeepSeekModel(settings);system='保留规则';user='问题含"转义"、中文与条件\n不得遗漏'
        if call_style=='positional':reply=model.generate(system,user)
        elif call_style=='keyword':reply=model.generate(user_prompt=user,system_prompt=system)
        else:reply=model.generate(system,user_prompt=user)
        assert reply=='合成回复'
    assert len(sent)==1 and sent[0]['max_tokens']==128
    assert sent[0]['thinking']=={'type':'enabled'} and sent[0]['reasoning_effort']=='high'
    row=rows(repo)[0];meta=json.loads(row['metadata_json'])
    assert row['request_hash']==fingerprint(sent[0])
    assert row['charged_units']==50 and row['state']=='reported'
    assert row['task_key']=='knowledge:fixture' and row['owner_key']=='alice'
    assert meta['counting_status']=='estimated' and meta['occupancy_percent'] is None
    assert '保留规则' not in row['metadata_json'] and '不得遗漏' not in row['metadata_json']


@pytest.mark.parametrize('mutation',[{'unknown':1},{'task_limit':True},{'task_limit':0},
    {'user_daily_limit':20,'global_daily_limit':10},{'outputs':{'answer':513}},
    {'outputs':{'unbounded':100}},{'capabilities':{'deepseek':{'binding':'wrong'}}}])
def test_invalid_server_policy_fails_at_configuration_without_http(tmp_path,mutation):
    with pytest.raises(ValueError,match='DOCQA_CONTEXT_BUDGET_POLICY'):environment(tmp_path,**mutation)


@pytest.mark.parametrize('extra', [0,1])
def test_verified_fixture_counter_checks_input_output_and_safety_boundary_before_http(tmp_path,monkeypatch,extra):
    settings,repo=environment(tmp_path,outputs={'default':128},safety_tokens=16)
    key=('fixture-only','v1',binding(settings,'deepseek'))
    monkeypatch.setitem(COUNTERS,key,lambda request:50)
    rules={'outputs':{'default':128},'safety_tokens':16,'capabilities':{'deepseek':{
        'binding':key[2],'window':194-extra,'counter_id':key[0],'counter_version':key[1],'verification':'synthetic-fixture'}}}
    settings=replace(settings,context_budget_policy=json.dumps(rules));sent=sdk(monkeypatch)
    if extra:
        with pytest.raises(ModelError,match='超过已登记上下文'):DeepSeekModel(settings).generate('规则','问题')
        assert not sent and not rows(repo)
    else:
        DeepSeekModel(settings).generate('规则','问题')
        assert len(sent)==1 and json.loads(rows(repo)[0]['metadata_json'])['occupancy_percent']==100


@pytest.mark.parametrize('fault',['binding','counter'])
def test_model_gateway_or_tokenizer_mismatch_never_sends_or_expands(tmp_path,monkeypatch,fault):
    settings,repo=environment(tmp_path)
    cap={'binding':binding(settings,'deepseek'),'window':100000,'counter_id':'missing','counter_version':'v2','verification':'fixture'}
    if fault=='binding':cap['binding']='0'*64
    settings=replace(settings,context_budget_policy=json.dumps({'capabilities':{'deepseek':cap}}))
    sent=sdk(monkeypatch)
    with pytest.raises(ModelError) as caught:DeepSeekModel(settings).generate('规则','问题')
    assert caught.value.not_sent and not sent and not rows(repo)


def test_atomic_account_global_and_task_reservations_do_not_overspend(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path,outputs={'default':32})
    amount=generation_request(settings,'规则','问题')['reserved_units']
    settings=replace(settings,context_budget_policy=json.dumps({'outputs':{'default':32},'task_limit':amount,
        'user_daily_limit':amount,'global_daily_limit':amount}))
    sent=sdk(monkeypatch)
    def run(owner):
        with owner_scope(owner),task_scope('knowledge',owner):
            try:DeepSeekModel(settings).generate('规则','问题');return 'sent'
            except ModelError as exc:assert exc.not_sent;return 'denied'
    with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(run,['alice','bob']))
    assert sorted(result)==['denied','sent'] and len(sent)==1
    assert sum(r['charged_units'] for r in rows(repo))==amount
    with repo.connect() as db:assert db.execute('SELECT COUNT(*) FROM provider_guard_calls').fetchone()[0]==1


def test_unknown_usage_and_restart_do_not_refund_or_reset_frozen_task_budget(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path,outputs={'default':32})
    amount=generation_request(settings,'规则','问题')['reserved_units']
    settings=replace(settings,context_budget_policy=json.dumps({'outputs':{'default':32},'task_limit':amount}))
    sent=sdk(monkeypatch)
    with task_scope('knowledge','restart'),owner_scope('alice'):DeepSeekModel(settings).generate('规则','问题')
    assert rows(repo)[0]['state']=='unknown' and rows(repo)[0]['charged_units']==amount
    Repository(repo.path).initialize()
    with task_scope('knowledge','restart'),owner_scope('alice'):
        with pytest.raises(ModelError,match='额度不足'):DeepSeekModel(settings).generate('规则','问题')
    assert len(sent)==1 and len(rows(repo))==1


def test_only_proven_not_sent_releases_both_request_and_token_reservations(tmp_path):
    settings,repo=environment(tmp_path);request=generation_request(settings,'规则','问题')
    with task_scope('knowledge','stopped'):
        with pytest.raises(ModelError):
            with provider_guard(settings,'deepseek',ModelError,delivery_tracked=True,budget_request=request):
                raise ModelError(409,'synthetic-stop-before-send')
    row=rows(repo)[0];assert row['state']=='not_sent' and row['charged_units']==0
    with repo.connect() as db:assert db.execute('SELECT status FROM provider_guard_calls').fetchone()[0]=='not_sent'


def test_timeout_keeps_uncertain_reservation_and_sdk_does_not_retry(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path)
    def timeout(request,payload):raise httpx.ReadTimeout('synthetic',request=request)
    sent=sdk(monkeypatch,handler=timeout)
    with task_scope('question','timeout'):
        with pytest.raises(ModelError):DeepSeekModel(settings).generate('规则','问题')
    assert len(sent)==1 and rows(repo)[0]['state']=='uncertain' and rows(repo)[0]['charged_units']>0


def test_unknown_partial_or_inconsistent_usage_is_not_reported_as_zero(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path)
    sdk(monkeypatch,{'prompt_tokens':40,'completion_tokens':10,'total_tokens':49})
    DeepSeekModel(settings).generate('规则','问题')
    assert rows(repo)[0]['state']=='unknown' and rows(repo)[0]['charged_units']==rows(repo)[0]['reserved_units']


def test_reported_overrun_is_recorded_as_debt_and_stops_later_task_steps(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path)
    amount=generation_request(settings,'规则','问题')['reserved_units']
    sent=sdk(monkeypatch,{'prompt_tokens':amount,'completion_tokens':1,'total_tokens':amount+1})
    with task_scope('analysis','overrun'):
        DeepSeekModel(settings).generate('规则','问题')
        with pytest.raises(ModelError,match='实报超出预留'):DeepSeekModel(settings).generate('规则','问题')
    assert len(sent)==1 and rows(repo)[0]['state']=='overrun' and rows(repo)[0]['charged_units']==amount+1


def test_policy_change_cannot_reinterpret_existing_task_or_drop_held_usage(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path);sent=sdk(monkeypatch)
    with task_scope('knowledge','frozen'):DeepSeekModel(settings).generate('规则','问题')
    changed=replace(settings,context_budget_policy='{"task_limit":3000000}')
    with task_scope('knowledge','frozen'):
        with pytest.raises(ModelError,match='配置或归属已变化'):DeepSeekModel(changed).generate('规则','问题')
    assert len(sent)==1 and rows(repo)[0]['charged_units']>0


def test_legacy_unknown_usage_is_retained_in_daily_admission_and_owner_status(tmp_path,monkeypatch):
    from app.resource_governance import stamp
    settings,repo=environment(tmp_path,user_daily_limit=1000)
    with repo.connect() as db:
        db.execute("INSERT INTO provider_guard_calls(id,owner_key,provider_key,status,started_at,deadline) VALUES ('old','alice','legacy','uncertain',?,?)",(stamp(),stamp()))
    sent=sdk(monkeypatch)
    with owner_scope('alice'):
        with pytest.raises(ModelError,match='额度不足'):DeepSeekModel(settings).generate('规则','问题')
    with repo.connect() as db:
        alice=status(db,settings,owner='alice');bob=status(db,settings,owner='bob')
    assert alice['legacy_calls']==alice['unknown_calls']==1 and alice['held_units']>0
    assert bob['legacy_calls']==bob['charged_units']==0 and not sent


def test_migration19_preserves_18_rows_and_failure_rolls_back(tmp_path):
    from app import migrations
    from unittest.mock import patch
    with patch.object(migrations,'SCHEMA_VERSION',18):
        repo=Repository(tmp_path/'source/docqa.db');repo.initialize()
    with repo.connect() as db:
        db.execute("INSERT INTO provider_guard_calls(id,owner_key,provider_key,status,started_at,deadline) VALUES ('old','local','old','uncertain','old','old')")
        columns=','.join(r[1] for r in db.execute('PRAGMA table_info(provider_guard_calls)'))
        before=tuple(db.execute('SELECT * FROM provider_guard_calls').fetchone())
    migrations.MigrationHook.register(19,lambda:(_ for _ in ()).throw(RuntimeError('synthetic19')))
    try:
        with pytest.raises(migrations.MigrationError):repo.initialize()
    finally:migrations.MigrationHook.clear()
    with repo.connect() as db:
        assert migrations.validate_schema(db,version=18)==18
        assert tuple(db.execute('SELECT * FROM provider_guard_calls').fetchone())==before
    from app.data_backup import backup,restore
    backup(tmp_path/'source',tmp_path/'backup');restore(tmp_path/'backup',tmp_path/'restored')
    assert Repository(tmp_path/'restored/docqa.db').initialize()['applied']==[19]
    with repo.connect() as db:assert migrations.validate_schema(db,version=18)==18
    assert repo.initialize()['applied']==[19]
    with repo.connect() as db:
        assert tuple(db.execute('SELECT '+columns+' FROM provider_guard_calls').fetchone())==before
        assert db.execute('SELECT meter_kind FROM provider_guard_calls').fetchone()[0] is None
        assert not db.execute('SELECT 1 FROM context_reservations').fetchone()
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_legacy_status_is_unknown_and_real_adapter_refuses_unmigrated_database(tmp_path,monkeypatch):
    from app import migrations
    from unittest.mock import patch
    settings=Settings(data_dir=tmp_path,deepseek_api_key='offline-only')
    with patch.object(migrations,'SCHEMA_VERSION',18):
        repo=Repository(tmp_path/'docqa.db');repo.initialize()
    with repo.connect() as db:assert status(db,settings)['reported_tokens'] is None
    sent=sdk(monkeypatch)
    with pytest.raises(ModelError,match='受控升级') as error:DeepSeekModel(settings).generate('规则','问题')
    assert error.value.not_sent and not sent


def test_utc_day_does_not_reset_task_and_new_day_retains_old_tombstones(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path,outputs={'default':32})
    amount=generation_request(settings,'规则','问题')['reserved_units']
    settings=replace(settings,context_budget_policy=json.dumps({'outputs':{'default':32},'task_limit':amount,
        'user_daily_limit':amount,'global_daily_limit':amount}))
    sent=sdk(monkeypatch)
    with task_scope('knowledge','old'),owner_scope('alice'):DeepSeekModel(settings).generate('规则','问题')
    with repo.connect() as db:
        db.execute("UPDATE context_reservations SET day='2000-01-01'")
        db.execute("UPDATE provider_guard_calls SET started_at='2000-01-01T00:00:00+00:00'")
    with task_scope('knowledge','old'),owner_scope('alice'):
        with pytest.raises(ModelError):DeepSeekModel(settings).generate('规则','问题')
    with task_scope('knowledge','new'),owner_scope('alice'):DeepSeekModel(settings).generate('规则','问题')
    with repo.connect() as db:
        assert status(db,settings,owner='alice')['charged_units']==amount
        assert status(db,settings,task_key='knowledge:old')['charged_units']==amount
    assert len(sent)==2 and len(rows(repo))==2


def test_expired_inflight_metadata_becomes_uncertain_without_releasing_units(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path);sent=sdk(monkeypatch)
    DeepSeekModel(settings).generate('规则','问题');first=rows(repo)[0]
    with repo.connect() as db:
        db.execute("UPDATE provider_guard_calls SET status='intent',deadline='2000-01-01'")
        db.execute("UPDATE context_reservations SET state='reserved'")
    DeepSeekModel(settings).generate('规则','问题')
    assert rows(repo)[0]['state']=='uncertain' and rows(repo)[0]['charged_units']==first['charged_units']
    assert len(sent)==2


def test_general_history_removes_only_whole_old_turns_and_keeps_current_conditions(tmp_path):
    from app.context_budget import fit_general_history
    settings,_=environment(tmp_path,outputs={'general_dispatch':32},safety_tokens=0)
    history=[{'source_turn':i,'question':'条件'+str(i),'answer':'旧原话'*100} for i in range(1,4)]
    def make_user(h):return dump({'question':'必须保留否定与2026年条件','notes':'用户明确限制','history':h})
    with profile_scope('general_dispatch'):
        two=generation_request(settings,'完整系统',make_user(history[1:]),stream=True)['reserved_units']
    settings=replace(settings,context_budget_policy=json.dumps({'outputs':{'general_dispatch':32},'safety_tokens':0,'product_window':two}))
    user,reduction=fit_general_history(settings,'完整系统',make_user,history)
    assert json.loads(user)['history']==history[1:] and reduction['removed_source_turns']==[1]
    assert '必须保留否定与2026年条件' in user and '用户明确限制' in user
    assert len(history)==3 and reduction['model_summary_calls']==0
    with pytest.raises(BudgetError):fit_general_history(replace(settings,context_budget_policy='{"product_window":1}'),'完整系统',make_user,history)


def test_cached_and_reasoning_tokens_are_not_added_twice_to_reported_total(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path)
    sdk(monkeypatch,{'prompt_tokens':40,'completion_tokens':10,'total_tokens':50,
        'prompt_tokens_details':{'cached_tokens':20},'completion_tokens_details':{'reasoning_tokens':8}})
    DeepSeekModel(settings).generate('规则','问题')
    assert rows(repo)[0]['charged_units']==50


@pytest.mark.parametrize('call_style',['positional','keyword','mixed'])
def test_stream_and_embedding_batches_share_task_and_charge_each_http(tmp_path,monkeypatch,call_style):
    from app.embedding import APIEmbedding
    from openai import OpenAI
    settings,repo=environment(tmp_path)
    settings=replace(settings,embedding_api_key='offline-only',embedding_batch_size=2)
    sent=[]
    def respond(request):
        body=json.loads(request.content);sent.append(body)
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200,json={'object':'list','model':body['model'],'data':[
                {'object':'embedding','index':i,'embedding':[1.0,0.0]} for i in range(len(body['input']))],
                'usage':{'prompt_tokens':5,'total_tokens':5}})
        chunks=[{'id':'fixture-stream','object':'chat.completion.chunk','created':0,'model':body['model'],
            'choices':[{'index':0,'delta':{'content':'合成流式'},'finish_reason':None}]},
            {'id':'fixture-stream','object':'chat.completion.chunk','created':0,'model':body['model'],
            'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}}]
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
    def factory(**kw):return OpenAI(http_client=httpx.Client(transport=httpx.MockTransport(respond)),**kw)
    monkeypatch.setattr('app.embedding.OpenAI',factory);monkeypatch.setattr('app.deepseek.OpenAI',factory)
    fragments=[]
    with task_scope('knowledge','shared'),owner_scope('alice'):
        assert len(APIEmbedding(settings).embed(['第一段','第二段','第三段']))==3
        model=DeepSeekModel(settings)
        if call_style=='positional':reply=model.generate_stream('规则','问题',fragments.append)
        elif call_style=='keyword':reply=model.generate_stream(on_text=fragments.append,user_prompt='问题',system_prompt='规则')
        else:reply=model.generate_stream('规则',user_prompt='问题',on_text=fragments.append)
        assert reply=='合成流式'
    records=rows(repo)
    assert len(sent)==len(records)==3 and {r['task_key'] for r in records}=={'knowledge:shared'}
    assert sum(r['charged_units'] for r in records)==23 and fragments==['合成流式']


@pytest.mark.parametrize('method',['generate','generate_stream'])
def test_invalid_python_arguments_never_create_a_reservation_or_send(tmp_path,monkeypatch,method):
    settings,repo=environment(tmp_path);sent=sdk(monkeypatch)
    with pytest.raises(TypeError) as caught:
        getattr(DeepSeekModel(settings),method)(system_prompt='规则',unknown_prompt='问题')
    assert caught.value.not_sent and not sent and not rows(repo)
    with repo.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM provider_guard_calls').fetchone()[0]==0


from tests.test_product_access import team


def test_quota_and_task_reads_are_owner_scoped_and_never_trigger_a_model(team,monkeypatch):
    env,users,clients=team
    sent=sdk(monkeypatch,{'prompt_tokens':40,'completion_tokens':10,'total_tokens':50})
    with owner_scope(users['alice']['id']):DeepSeekModel(env.settings).generate('不应展示的规则','私有问题不进用量')
    for _ in range(2):
        alice=clients['alice'].get('/api/chat/quota');bob=clients['bob'].get('/api/chat/quota')
        admin=clients['admin'].get('/api/admin/operations')
        assert alice.status_code==bob.status_code==admin.status_code==200
        assert alice.json()['context_budget']['reported_tokens']==50
        assert bob.json()['context_budget']['charged_units']==0
        assert admin.json()['context_budget']['reported_tokens']==50
        assert '私有问题' not in alice.text+admin.text and '不应展示' not in admin.text
    assert len(sent)==1


def test_tool_http_uses_daily_request_quota_without_creating_fake_model_tokens(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path);settings=replace(settings,calls_user_daily_limit=1)
    with owner_scope('alice'),task_scope('knowledge','tool'):
        with provider_guard(settings,'mcp',ModelError,delivery_tracked=True,provider_identity='isolated-tool',timeout_seconds=30):
            mark_provider_sent();mark_provider_completed()
        sent=sdk(monkeypatch)
        with pytest.raises(ModelError,match='本日调用配额'):DeepSeekModel(settings).generate('规则','问题')
    with repo.connect() as db:
        usage=status(db,settings,owner='alice')
        assert db.execute('SELECT meter_kind FROM provider_guard_calls').fetchone()[0]=='tool_http'
    assert usage['unknown_calls']==usage['charged_units']==usage['legacy_calls']==0
    assert usage['verified_cost'] is None and not sent and not rows(repo)


def test_changed_model_cannot_continue_a_frozen_token_task(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path);sent=sdk(monkeypatch)
    with task_scope('knowledge','model-frozen'):DeepSeekModel(settings).generate('规则','问题')
    with task_scope('knowledge','model-frozen'):
        with pytest.raises(ModelError,match='配置或归属已变化'):
            DeepSeekModel(replace(settings,deepseek_model='another-model')).generate('规则','问题')
    assert len(sent)==1 and len(rows(repo))==1


def test_unused_output_reserve_cannot_hide_bad_verified_input_counter_across_tasks(tmp_path,monkeypatch):
    settings,repo=environment(tmp_path)
    key=('fixture-counter','v1',binding(settings,'deepseek'))
    monkeypatch.setitem(COUNTERS,key,lambda request:1)
    cap={'binding':key[2],'window':10000,'counter_id':key[0],'counter_version':key[1],'verification':'synthetic-fixture'}
    settings=replace(settings,context_budget_policy=json.dumps({'capabilities':{'deepseek':cap}}))
    sent=sdk(monkeypatch,{'prompt_tokens':40,'completion_tokens':10,'total_tokens':50})
    with task_scope('knowledge','bad-count'):DeepSeekModel(settings).generate('规则','问题')
    first=rows(repo)[0]
    assert first['charged_units']==50 and first['charged_units']<first['reserved_units'] and first['state']=='overrun'
    with task_scope('knowledge','different-task'):
        with pytest.raises(ModelError,match='计数版本已有实报偏差'):DeepSeekModel(settings).generate('规则','问题')
    cap={**cap,'counter_version':'v2'};monkeypatch.setitem(COUNTERS,(key[0],'v2',key[2]),lambda request:40)
    corrected=replace(settings,context_budget_policy=json.dumps({'capabilities':{'deepseek':cap}}))
    with task_scope('knowledge','explicit-new-version'):DeepSeekModel(corrected).generate('规则','问题')
    assert len(sent)==2 and len(rows(repo))==2 and rows(repo)[1]['state']=='reported'
