"""新范围沿用原累计账本；真实SDK/生产问答，HTTP使用可计数的本地桩。"""
import json
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.evaluation_budget import BudgetTransport, EvaluationBudget, EvaluationStopped
from app.rag_evaluation import digest
from scripts import evaluate_rag_acceptance_live as live
from scripts.evaluate_rag_targeted_live import CONFIG_FIELDS, save, file_hash
from tests.test_question_jobs import REPLY
from tests.test_rag_targeted_live import scope as parent_scope


def scope(tmp_path, monkeypatch):
    parent=parent_scope(tmp_path,monkeypatch)
    monkeypatch.setattr(live,'ROOT',tmp_path)
    frozen={'synthetic':'preserved approval fingerprint'}
    save(parent/'runtime-freeze.json',frozen)
    budget=EvaluationBudget(parent/'request-ledger.db',limit=40,fingerprint=digest(frozen))
    for i in range(2):
        number=budget.reserve(role='generation',context='previous:'+str(i),content=b'old')
        budget.settle(number,status='succeeded',elapsed_ms=1,http_status=200)
    directory=tmp_path/'data'/'acceptance';directory.mkdir()
    # 独立SQLite副本，避免评估脚本修改前阶段业务库。
    import sqlite3
    (directory/'run-data').mkdir()
    with sqlite3.connect(parent/'run-data/docqa.db') as src, sqlite3.connect(directory/'run-data/docqa.db') as dst:
        src.backup(dst)
    cases=json.loads((parent/'cases.json').read_text(encoding='utf-8'))[:2]
    settings=Settings.from_env()
    auth=dict(total_request_limit=40,no_new_budget=True,user_reply='Synthetic approval for offline test',
        parent_directory=parent.name,parent_approval_sha256=file_hash(parent/'approval.json'),
        parent_freeze_sha256=file_hash(parent/'runtime-freeze.json'),parent_ledger_rows=2,
        parent_ledger_rows_sha256=digest(budget.report()['calls']))
    save(directory/'authorization-extension.json',auth)
    plan=dict(schema='docqa-rag-acceptance-v1',limit=40,budget_parent=parent.name,
        targets=[case['case_id'] for case in cases],cases_sha256=digest(cases),documents=[],
        config={name:getattr(settings,name) for name in CONFIG_FIELDS},
        question_retrieval_strategy=settings.question_retrieval_strategy,
        planned_requests=dict(query=2,index=0,generation=2,total=4))
    for name,value in [('plan.json',plan),('cases.json',cases),('paper-semantic-rubric.json',{}),
                       ('additional-source-rubric.json',{})]: save(directory/name,value)
    save(directory/'scope-freeze.json',dict(plan_sha256=digest(plan),cases_sha256=digest(cases),
        authorization_sha256=file_hash(directory/'authorization-extension.json'),
        paper_rubric_sha256=file_hash(directory/'paper-semantic-rubric.json'),
        source_rubric_sha256=file_hash(directory/'additional-source-rubric.json')))
    return directory,parent,budget


def provider(monkeypatch,status=200):
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        if status!=200: return httpx.Response(status,json={'error':{'message':'synthetic'}})
        if 'input' in body:
            return httpx.Response(200,json={'object':'list','model':'test','data':[
                {'object':'embedding','index':i,'embedding':[1.0,0.0]} for i in range(len(body['input']))],
                'usage':{'prompt_tokens':5,'total_tokens':5}})
        return httpx.Response(200,json={'id':'synthetic-answer','object':'chat.completion','created':1,'model':'test',
            'choices':[{'index':0,'message':{'role':'assistant','content':REPLY},
                        'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}})
    original=BudgetTransport.__init__
    def init(self,budget,**kwargs):
        original(self,budget,**kwargs,inner=httpx.MockTransport(handler))
    monkeypatch.setattr(BudgetTransport,'__init__',init)
    return calls


def test_default_plan_does_not_load_env_or_send_http(tmp_path,monkeypatch):
    directory,parent,budget=scope(tmp_path,monkeypatch)
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls: pytest.fail('plan must not load environment')))
    calls=provider(monkeypatch)
    assert live.run(directory)['used']==2
    assert live.run(directory,phase='answer')['provider_calls_this_run']==0
    assert budget.report()['used']==2 and not calls


def test_retrieve_preview_generate_and_repeat_use_same_ledger(tmp_path,monkeypatch):
    directory,parent,budget=scope(tmp_path,monkeypatch);calls=provider(monkeypatch)
    assert live.run(directory,phase='retrieve',allow_online=True)['used']==4
    assert live.run(directory,phase='retrieve',allow_online=True)['used']==4
    assert live.run(directory,phase='preview',allow_online=True)['used']==4
    result=live.run(directory,phase='answer',allow_online=True)
    assert result['used']==6 and len(calls)==4
    assert live.run(directory,phase='answer',allow_online=True)['used']==6
    assert not (directory/'request-ledger.db').exists()
    report=budget.report()
    assert [c['role'] for c in report['calls']]==['generation','generation','query','query','generation','generation']
    assert 'semantic_rubric' not in json.dumps(calls[-1])


@pytest.mark.parametrize('damage',[None,'same_input','not_strict','wrong_case','wrong_answer','no_result','bad_revision'])
def test_regression_requires_bound_semantic_pass_and_changed_actual_input(tmp_path,damage):
    case=dict(case_id='stock:VALUES',frozen_source='fixture')
    source=tmp_path/'runs'/'old';(source/'outputs').mkdir(parents=True);(source/'reviews').mkdir()
    previous=dict(job=dict(status='succeeded'),result=dict(answer=dict(conclusion=[])))
    before=dict(request=dict(system='old',user=dict(question='test')))
    review=dict(strict_pass=True,case_id=case['case_id'],case_sha256=digest(case),
                answer_sha256=digest(previous['result']['answer']))
    current=dict(system='new shared protocol',user=dict(question='test'))
    if damage=='same_input':current=before['request']
    elif damage=='not_strict':review['strict_pass']=False
    elif damage=='wrong_case':review['case_sha256']='changed source'
    elif damage=='wrong_answer':review['answer_sha256']='different result'
    elif damage=='no_result':previous['result']={}
    for path,value in [(source/'outputs/stock-VALUES.result.json',previous),
                       (source/'outputs/stock-VALUES.preview.json',before),
                       (source/'reviews/stock-VALUES.json',review)]:save(path,value)
    if damage:
        with pytest.raises(EvaluationStopped):live.regression_files(tmp_path,case,'../old' if damage=='bad_revision' else 'old',current)
    else:
        bound=live.regression_files(tmp_path,case,'old',current)
        assert len(bound)==3 and all(file_hash(tmp_path/name)==value for name,value in bound.items())


@pytest.mark.parametrize('damage',['missing_ledger','old_row','case','rubric','approval'])
def test_changed_or_missing_frozen_inputs_stop_before_network(tmp_path,monkeypatch,damage):
    directory,parent,budget=scope(tmp_path,monkeypatch);calls=provider(monkeypatch)
    if damage=='missing_ledger': (parent/'request-ledger.db').unlink()
    elif damage=='old_row':
        with budget.connect() as db: db.execute("UPDATE calls SET context='tampered' WHERE sequence=1")
    else:
        name={'case':'cases.json','rubric':'paper-semantic-rubric.json','approval':'authorization-extension.json'}[damage]
        (directory/name).write_text('{}',encoding='utf-8')
    with pytest.raises((EvaluationStopped,TypeError)):
        live.run(directory,phase='retrieve',allow_online=True)
    assert not calls
    if damage=='missing_ledger': assert not (parent/'request-ledger.db').exists()


def test_provider_failure_is_counted_once_and_stops_resume(tmp_path,monkeypatch):
    directory,parent,budget=scope(tmp_path,monkeypatch);calls=provider(monkeypatch,status=402)
    with pytest.raises(Exception): live.run(directory,phase='retrieve',allow_online=True)
    assert budget.report()['used']==3 and len(calls)==1
    with pytest.raises(EvaluationStopped): live.run(directory,phase='retrieve',allow_online=True)
    assert budget.report()['used']==3 and len(calls)==1


def test_changed_configuration_or_unknown_target_stops_before_network(tmp_path,monkeypatch):
    directory,parent,budget=scope(tmp_path,monkeypatch);calls=provider(monkeypatch)
    with pytest.raises(EvaluationStopped):
        live.run(directory,phase='retrieve',allow_online=True,targets=['other'])
    env=Settings.from_env()
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls: replace(env,rag_context_max_chars=11999)))
    with pytest.raises(EvaluationStopped): live.run(directory,phase='retrieve',allow_online=True)
    assert budget.report()['used']==2 and not calls


def test_renaming_revision_cannot_authorize_a_repeated_request(tmp_path,monkeypatch):
    directory,parent,budget=scope(tmp_path,monkeypatch);calls=provider(monkeypatch)
    with pytest.raises(FileNotFoundError):
        live.run(directory,phase='retrieve',allow_online=True,revision='renamed')
    assert budget.report()['used']==2 and not calls


def test_source_change_rejects_saved_query_before_generation(tmp_path,monkeypatch):
    directory,parent,budget=scope(tmp_path,monkeypatch);calls=provider(monkeypatch)
    live.run(directory,phase='retrieve',allow_online=True)
    from app.repository import Repository
    repo=Repository(directory/'run-data/docqa.db')
    with repo.connect() as db:
        db.execute("UPDATE chunks SET text='changed original' WHERE document_id='sheet'")
    with pytest.raises(EvaluationStopped): live.run(directory,phase='answer',allow_online=True)
    assert budget.report()['used']==4 and len(calls)==2


@pytest.mark.parametrize('damage',[None,'valid80','no_reply','wrong_limit','missing_backup','old_calls',
                                  'unapproved_row','too_many','bool_extra','negative_previous','bool_previous'])
def test_one_explicit_extension_keeps_same_ledger_and_fails_closed(tmp_path,monkeypatch,damage):
    import sqlite3
    from contextlib import closing
    directory,parent,budget=scope(tmp_path,monkeypatch)
    before=budget.report()['calls']
    backup=directory/'ledger-before-extension.db'
    with closing(sqlite3.connect(parent/'request-ledger.db')) as src,closing(sqlite3.connect(backup)) as dst: src.backup(dst)
    approval=dict(authorized=True,user_reply='Synthetic explicit request extension approval',previous_limit=40,
        additional_requests=1,total_request_limit=41,scope_sha256=file_hash(directory/'scope-freeze.json'),
        previous_approval_sha256=file_hash(parent/'approval.json'),ledger_backup_sha256=file_hash(backup),
        previous_used=2,previous_calls_sha256=digest(before))
    with budget.connect() as db: db.execute('UPDATE approval SET max_requests=41 WHERE id=1')
    if damage=='valid80':
        approval.update(additional_requests=40,total_request_limit=80)
        with budget.connect() as db: db.execute('UPDATE approval SET max_requests=80 WHERE id=1')
    elif damage=='no_reply': approval['user_reply']=''
    elif damage=='wrong_limit': approval['total_request_limit']=42
    elif damage=='missing_backup': backup.unlink()
    elif damage=='old_calls':
        with budget.connect() as db: db.execute("UPDATE calls SET context='modified' WHERE sequence=1")
    elif damage=='unapproved_row':
        with budget.connect() as db: db.execute('UPDATE approval SET max_requests=40 WHERE id=1')
    elif damage=='too_many': approval.update(additional_requests=41,total_request_limit=81)
    elif damage=='bool_extra': approval['additional_requests']=True
    elif damage=='negative_previous': approval['previous_used']=-1
    elif damage=='bool_previous': approval['previous_used']=True
    save(directory/'budget-extension.json',approval)
    if damage not in {None,'valid80'}:
        with pytest.raises(EvaluationStopped): live.load_scope(directory)
    else:
        extended=live.load_scope(directory)[-1]
        assert extended.path==budget.path and extended.report()['calls']==before
        number=extended.reserve(role='generation',context='new',content=b'new')
        assert number==3 and extended.report()['limit']==(80 if damage=='valid80' else 41)
        extended.settle(number,status='succeeded',elapsed_ms=1,http_status=200)
        assert extended.report()['calls'][:2]==before
    assert not (directory/'request-ledger.db').exists()
