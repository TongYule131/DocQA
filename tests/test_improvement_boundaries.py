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
        assert migrations.run_migrations(db)['applied']==[15]
        assert migrations.run_migrations(db)['applied']==[]
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def frozen_case(env,access,base):
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'frozen')
    task=service.submit(conv['id'],'退货包装有什么要求','question')
    payload=json.loads(service.tasks.row(task['id'])['input_json']);documents=[]
    for doc in payload['documents']:
        documents.append({**doc,'chunks':[c.model_dump() for c in env.repository.chunks(doc['document_id'],doc['snapshot']['parse_version_id'])],
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
