"""审核隐私、供应商并发、冻结回放、监管恢复及 v14→v15 原子迁移。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from app import migrations
from app.config import Settings
from app.frozen_retrieval import freeze,evaluate
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_tasks import KnowledgeTasks
from app.resource_governance import provider_guard,owner_scope
from app.retrieval_channels import fingerprint,ReplayReranker,apply_reranker
from tests.test_knowledge_chain import library
from tests.test_product_access import team,publish
from tests.test_question_jobs import QUESTION


def test_full_conversation_retains_early_comparison_entities(tmp_path):
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'long')
    from app.entity_memory import context
    from app.repository import now_iso
    with env.repository.connect() as db:
        for number in range(1,101):
            question='比较“早期甲”与“早期乙”的条件' if number==1 else '继续解释它们的适用条件'
            payload={'question':question,'memory_epoch':0,'route':{'kind':'KB'}};identifier='history-'+str(number)
            db.execute("INSERT INTO kb_tasks(id,kind,base_id,owner_user_id,request_key,input_json,input_hash,max_requests,created_at,updated_at,status) VALUES (?,'question',?,?,?,?,?,8,?,?,'succeeded')",
                (identifier,base['id'],None,'synthetic-'+str(number),json.dumps(payload),fingerprint(payload),now_iso(),now_iso()))
            db.execute('INSERT INTO kb_conversation_turns VALUES (?,?,?)',(conv['id'],number,identifier))
        result=context(db,conv,env.settings)
    assert [e['name'] for e in result['entities']]==['早期甲','早期乙']
    assert result['comparison_objects']==['早期甲','早期乙']
    assert all(e['source_turn']==1 for e in result['entities'])


def test_analysis_quota_and_provider_scope_follow_submitting_admin(tmp_path):
    from app.analysis_jobs import AnalysisJobService
    from app.repository import Repository
    from app.product_access import ProductAccess,actor_context
    from app.resource_governance import task_owner,CALL_OWNER
    from app.schemas import AnalysisJobRequest
    from tests.test_analysis import fixture_settings,seed_document
    settings=fixture_settings(tmp_path,deepseek_api_key='synthetic-only',active_global_limit=2,queue_user_limit=1)
    repository=Repository(tmp_path/'docqa.db');document,_=seed_document(repository)
    access=ProductAccess(repository,settings,None)
    users=[access.create_user('admin-'+str(i),'管理员','Synthetic-password-2026','admin') for i in range(2)]
    service=AnalysisJobService(settings,repository);created=[]
    for user,kind in zip(users,('extraction','summary')):
        token=actor_context.set(user)
        try:
            response,_=service.submit(document,kind,AnalysisJobRequest(idempotency_key='analysis-'+kind))
            created.append(response.job)
        finally:actor_context.reset(token)
    claimed=[repository.claim_next_analysis_job('worker-'+str(i),30,job_id=job.id) for i,job in enumerate(created)]
    assert all(claimed),'两个管理员不能因共用local配额桶互相阻塞'
    class Probe:
        def __init__(self):self.repository=repository
        @task_owner
        def inspect(self,job):return CALL_OWNER.get()
    assert [Probe().inspect(job) for job in claimed]==[user['id'] for user in users]
    with repository.connect() as db:
        assert {r[0] for r in db.execute('SELECT owner_user_id FROM analysis_jobs')}=={user['id'] for user in users}


def test_legacy_bridge_keeps_user_entities_with_original_conversation_provenance(tmp_path,monkeypatch):
    from tests.test_unified_knowledge import context as fixture
    from app.query_understanding import build_memory
    from app.conversation_memory import MEMORY_VERSION
    env,access,base,questions,chat=fixture(tmp_path)
    access.bind_document(base['id'],'qdoc');access.publish(base['id'],0)
    old=chat.legacy.create('qdoc','legacy')
    memory={'version':MEMORY_VERSION,'notes':'用户条件','recent_questions':['最近一问'],
            'entity_context':build_memory([{'turn':1,'question':'比较“历史甲”与“历史乙”的差异'}])}
    monkeypatch.setattr(chat.legacy,'memory',lambda original:{'context':memory})
    identifier=chat.bridge('d:'+old['id']);detail=questions.detail(identifier)
    assert [e['name'] for e in detail['entity_memory']['entities']]==['历史甲','历史乙']
    assert all(e['source_turn'].startswith('d:'+old['id']+':turn:') for e in detail['entity_memory']['entities'])
    questions.memory(identifier,False,True)
    assert questions.detail(identifier)['entity_memory']['entities']==[]
    assert not env.model.calls


def test_optional_paid_planner_does_not_run_when_frozen_source_is_invalid(tmp_path):
    env,access,base=library(tmp_path);settings=replace(env.settings,query_planner_enabled=True)
    access.settings=settings;env.rag.settings=settings
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'preflight')
    task=service.submit(conv['id'],'退货条件','q')
    with env.repository.connect() as db:
        db.execute("UPDATE embedding_indexes SET source_signature='changed' WHERE document_id='policy'")
    from app.knowledge_worker import KnowledgeWorker
    from tests.test_question_jobs import LocalRuntime
    worker=KnowledgeWorker(settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    assert service.tasks.row(task['id'])['status']=='failed'
    assert not env.model.calls and not env.embedding.calls


def test_customer_feedback_consent_admin_projection_and_revoke_after_scope_revocation(team):
    env,users,clients=team;base=publish(clients,users);alice=clients['alice'];admin=clients['admin'];bob=clients['bob']
    job=alice.post('/api/documents/qdoc/question-jobs',json={'question':QUESTION,'idempotency_key':'feedback-job'}).json()['job']
    body={'source':'question','task_id':job['id'],'request_key':'feedback','category':'expression','comment':'客户私有说明','share_context':True}
    row=alice.post('/api/feedback',json=body);assert row.status_code==201,row.text
    identifier=row.json()['id']
    assert bob.get('/api/feedback/'+identifier).status_code==404
    assert bob.get('/api/admin/feedback').status_code==403
    assert QUESTION not in admin.get('/api/admin/feedback').text and '客户私有说明' not in admin.get('/api/admin/feedback').text
    assert admin.get('/api/admin/feedback/'+identifier).json()['shared_context']['question']==QUESTION
    admin.put(f"/api/knowledge-bases/{base['id']}/members/{users['alice']['id']}",json={'enabled':False})
    assert alice.delete('/api/feedback/'+identifier+'/shared-context').status_code==200
    assert 'shared_context' not in admin.get('/api/admin/feedback/'+identifier).json()
    assert 'context_json' not in alice.get('/api/feedback/'+identifier).json()
    assert not env.embedding.calls and not env.model.calls


def test_simultaneous_admission_never_exceeds_queue_limit(tmp_path):
    env,access,base=library(tmp_path);env.repository.settings=replace(env.settings,queue_user_limit=1)
    service=KnowledgeQuestions(access,env.rag)
    conversations=[service.create(base['id'],str(i)) for i in range(4)]
    barrier=Barrier(4)
    def submit(conv):
        barrier.wait()
        try:return service.submit(conv['id'],'原子配额','q')['id']
        except Exception as exc:return getattr(exc,'status_code',None)
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(submit,conversations))
    assert sum(isinstance(r,str) for r in results)==1 and results.count(429)==3
    assert not env.embedding.calls and not env.model.calls


def test_daily_quota_and_active_http_permits_cover_all_provider_roles(tmp_path):
    env,_,_=library(tmp_path);settings=replace(env.settings,calls_user_daily_limit=1)
    class Error(Exception):
        def __init__(self,code,message):super().__init__(message);self.status_code=code
    with owner_scope('same-user'):
        with provider_guard(settings,'embedding',Error):pass
        with pytest.raises(Error,match='本日') as refused:
            with provider_guard(settings,'deepseek',Error):pytest.fail('HTTP forbidden')
        assert refused.value.not_sent
    settings=replace(env.settings,active_global_limit=1)
    with owner_scope('user-a'):
        with provider_guard(settings,'embedding',Error):
            with owner_scope('user-b'):
                with pytest.raises(Error,match='并发'):
                    with provider_guard(settings,'deepseek',Error):pytest.fail('HTTP forbidden')
    # 全局仍有空位时，同一用户的生成与索引也不能绕过默认用户并发1。
    settings=replace(env.settings,active_global_limit=2)
    with owner_scope('same-owner'):
        with provider_guard(settings,'deepseek',Error):
            with pytest.raises(Error,match='并发') as refused:
                with provider_guard(settings,'embedding',Error):pytest.fail('same-user HTTP forbidden')
            assert refused.value.not_sent


def test_user_fairness_uses_last_started_not_only_oldest_task(tmp_path):
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag)
    a=service.create(base['id'],'a');b=service.create(base['id'],'b')
    first=service.submit(a['id'],'问题甲','a');second=service.submit(b['id'],'问题乙','b')
    user=access.create_user('fair-user','合成用户','Synthetic-password-2026','admin')
    with env.repository.connect() as db:
        db.execute('UPDATE kb_tasks SET owner_user_id=? WHERE id=?',(user['id'],second['id']))
        db.execute('INSERT INTO dispatch_fairness VALUES (?,?)',('local','9999'))
        from app.resource_governance import choose
        db.execute('BEGIN IMMEDIATE') if not db.in_transaction else None
        assert choose(db,env.repository,'kb_tasks')['id']==second['id']


def test_v15_failure_rolls_back_and_retry_preserves_v14_rows(tmp_path):
    path=tmp_path/'docqa.db'
    with closing(sqlite3.connect(path)) as db:
        db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        for i in range(1,15):
            migrations.MIGRATIONS[i](db);db.execute('INSERT INTO schema_migrations VALUES (?,?)',(i,'synthetic'))
        db.commit();tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        migrations.MigrationHook.register(15,lambda:(_ for _ in ()).throw(RuntimeError('synthetic migration fault')))
        try:
            with pytest.raises(migrations.MigrationError):migrations.run_migrations(db)
        finally:migrations.MigrationHook.clear()
        assert migrations.detect_version(db)==14
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}==tables
        assert migrations.validate_schema(db,version=14)==14
        assert migrations.run_migrations(db)['applied']==list(range(15,migrations.SCHEMA_VERSION+1))
        assert migrations.run_migrations(db)['applied']==[]
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def frozen_case(env,access,base):
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'frozen')
    task=service.submit(conv['id'],'退货包装有什么要求','question')
    payload=json.loads(service.tasks.row(task['id'])['input_json']);documents=[]
    for doc in payload['documents']:
        version=env.repository.get_parse_version(doc['snapshot']['parse_version_id'])
        with env.repository.connect() as db:
            vectors=[{'chunk_id':r['chunk_id'],'vector':json.loads(r['vector'])} for r in db.execute('SELECT chunk_id,vector FROM embedding_vectors_v2 WHERE index_id=?',(doc['snapshot']['index_id'],))]
        documents.append({**doc,'chunks':[c.model_dump() for c in env.repository.chunks(doc['document_id'],doc['snapshot']['parse_version_id'])],
            'vectors':vectors,'quality':{'quality_status':version.quality_status,'warnings':[w.model_dump() for w in version.warnings]},
            'vector_result':env.rag.index.search_snapshot(doc['document_id'],'退货包装有什么要求',8,snapshot=doc['snapshot'],include_scores=True)})
    return {'id':'synthetic-multi-doc','annotation_status':'synthetic','document_group':'synthetic-group-1',
            'question':'退货包装有什么要求','query_vector':[1,0], 'documents':documents,
            'budget':payload['retrieval_budget'],'required_groups':[{'all_of':['pc1']} ]}


def test_frozen_candidate_replay_preserves_visible_support_and_detects_changes(tmp_path):
    env,access,base=library(tmp_path);case=frozen_case(env,access,base)
    suite=freeze([case]);env.embedding.calls.clear();env.model.calls.clear()
    result=evaluate(suite)
    assert result['provider_calls']==0 and result['cases'][0]['visible_groups']==1
    assert result['cases'][0]['quoted_groups'] is None
    assert result['cases'][0]['annotation_status']=='synthetic'
    assert not env.embedding.calls and not env.model.calls
    case['question']='changed'
    with pytest.raises(ValueError,match='changed'):evaluate(suite)


def test_replay_reranker_requires_exact_candidate_fingerprint(tmp_path):
    candidates=[{'chunk_id':'one','text':'合成甲'},{'chunk_id':'two','text':'合成乙'}];path=tmp_path/'replay.json'
    key=fingerprint({'question':'合成问题','candidates':candidates})
    path.write_text(json.dumps({'version':ReplayReranker.version,'rankings':{key:['two','one']}}),encoding='utf-8')
    ranked,diagnostic=apply_reranker('合成问题',candidates,ReplayReranker(path))
    assert ranked[0]['chunk_id']=='two' and diagnostic['state']=='applied'
    ranked,diagnostic=apply_reranker('不同问题',candidates,ReplayReranker(path))
    assert ranked==candidates and diagnostic['state']=='fallback_original'


def test_supervisor_singleton_stop_marker_and_code_change_do_not_restart(tmp_path,monkeypatch):
    from app import local_supervisor as module
    directory=tmp_path/'.local-service';directory.mkdir()
    class Guard:
        def __init__(self,data,role):self.data_dir=data;self.fingerprint='synthetic'
        def check(self):raise RuntimeError('code changed')
    monkeypatch.setattr(module,'RuntimeGuard',Guard)
    calls=[]
    assert module.supervise(tmp_path,max_cycles=1,run_script=lambda *a:calls.append(a))==2
    assert not calls and json.loads((directory/'supervisor.json').read_text())['reason']=='code_or_schema_changed'
    (directory/'supervisor.stop').write_text('operator')
    assert module.supervise(tmp_path,max_cycles=1,run_script=lambda *a:calls.append(a))==0
    assert not calls
    with module.exclusive(directory/'test.lock'):
        with pytest.raises((OSError,BlockingIOError)):
            with module.exclusive(directory/'test.lock'):pytest.fail('second supervisor')


def test_supervisor_recovers_only_three_times_and_honors_manual_stop(tmp_path,monkeypatch):
    from app import local_supervisor as module
    class Guard:
        def __init__(self,data,role):self.data_dir=data;self.fingerprint='synthetic'
        def check(self):pass
    monkeypatch.setattr(module,'RuntimeGuard',Guard);monkeypatch.setattr(module,'roles_ready',lambda *a:False)
    calls=[]
    def run(name,*args):calls.append(name);return SimpleNamespace(returncode=0,stdout=b'')
    assert module.supervise(tmp_path,max_cycles=5,sleep=lambda _:None,run_script=run)==3
    assert calls==['stop_local.ps1','start_local.ps1']*3
    assert json.loads((tmp_path/'.local-service'/'supervisor.json').read_text())['reason']=='restart_limit'
