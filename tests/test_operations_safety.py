"""配额、容量、生命周期互斥及健康进展；使用真实SQLite与操作系统锁。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timezone
import io
import json
import os

from fastapi import UploadFile,HTTPException
import pytest

from app.config import Settings
from app.repository import Repository
from app.resource_governance import jobs
from app.knowledge_tasks import KnowledgeTasks
from app.runtime import RuntimeGuard,RuntimeMismatch
from app.maintenance import maintenance
from app.data_backup import backup
from app.local_supervisor import roles_ready
from app.uploads import store_upload


def env(tmp_path,**options):
    settings=Settings(data_dir=tmp_path,**options);repo=Repository(tmp_path/'docqa.db');repo.settings=settings;repo.initialize()
    return settings,repo


def test_maintenance_and_live_roles_exclude_each_other(tmp_path):
    _,repo=env(tmp_path)
    guard=RuntimeGuard(tmp_path,'web')
    with maintenance(tmp_path,'synthetic'):
        with pytest.raises(RuntimeMismatch):guard.prepare_database()
    guard.prepare_database()
    try:
        with pytest.raises(RuntimeError):backup(tmp_path,tmp_path.parent/(tmp_path.name+'-blocked-backup'))
    finally:guard.release_lifecycle()
    assert backup(tmp_path,tmp_path.parent/(tmp_path.name+'-allowed-backup'))['schema_version']==16


def test_queue_timeout_releases_quota_without_provider_calls(tmp_path):
    _,repo=env(tmp_path,queue_user_limit=1,queue_wait_seconds=1)
    tasks=KnowledgeTasks(repo)
    old=tasks.create('question',{'route':{'kind':'SYSTEM'}},'old')
    with repo.connect() as db:db.execute("UPDATE kb_tasks SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",(old['id'],))
    new=tasks.create('question',{'route':{'kind':'SYSTEM'}},'new')
    assert tasks.row(old['id'])['error_code']=='queue_timeout'
    assert tasks.row(old['id'])['requests_used']==0 and new['status']=='queued'
    with repo.connect() as db:assert db.execute('SELECT COUNT(*) FROM provider_guard_calls').fetchone()[0]==0


def test_original_storage_quota_is_atomic_and_completed_key_reuses(tmp_path):
    settings,repo=env(tmp_path,storage_user_mb=1,storage_global_mb=1,storage_free_min_mb=0)
    def upload(data,key):return store_upload(repo,settings,UploadFile(filename='synthetic.txt',file=io.BytesIO(data)),key)
    first=upload(b'a'*700000,'first')
    assert upload(b'a'*700000,'first')['document'].id==first['document'].id
    with pytest.raises(HTTPException) as full:upload(b'b'*700000,'second')
    assert full.value.status_code==429
    with repo.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM storage_reservations').fetchone()[0]==0
        assert db.execute('SELECT SUM(byte_size) FROM document_storage_owners').fetchone()[0]==700000
    assert len(list((tmp_path/'uploads').iterdir()))==1


def test_ingestion_to_parse_converts_one_queue_slot(tmp_path):
    from tests.test_unified_knowledge import context,upload
    from app.ingestion import IngestionCoordinator
    original,access,base,questions,chat=context(tmp_path)
    settings=replace(original.settings,queue_user_limit=1)
    original.settings=settings;access.settings=settings;original.repository.settings=settings
    job=upload(access,base)
    coordinator=IngestionCoordinator(settings,original.repository,embedding=original.embedding)
    coordinator.run_once(job['id'])
    with original.repository.connect() as db:
        pending=[r for r in jobs(db) if r['status']=='queued']
        assert len(pending)==1 and pending[0]['kind']=='parse_tasks'


def test_live_pid_with_stale_progress_is_not_ready(tmp_path):
    env(tmp_path)
    for role in ('web','parse-worker','analysis-worker'):RuntimeGuard(tmp_path,role).mark_ready()
    supervisor=RuntimeGuard(tmp_path,'supervisor');directory=tmp_path/'.local-service'
    assert roles_ready(directory,supervisor)
    path=directory/'analysis-worker.heartbeat';value=json.loads(path.read_text(encoding='utf-8'))
    value['checked_at']='2000-01-01T00:00:00+00:00';path.write_text(json.dumps(value))
    assert not roles_ready(directory,supervisor)
    status=RuntimeGuard(tmp_path,'web').service_status()
    worker=next(r for r in status['workers'] if r['role']=='analysis-worker')
    assert worker['running'] and not worker['ready']


def test_heartbeat_publication_is_safe_under_threads_and_windows_sharing(tmp_path,monkeypatch):
    env(tmp_path);guard=RuntimeGuard(tmp_path,'web');original=os.replace;attempts=[]
    def locked(src,dst):
        if str(dst).endswith('.heartbeat') and len(attempts)<2:
            attempts.append(dst);error=PermissionError('synthetic sharing');error.winerror=32;raise error
        return original(src,dst)
    monkeypatch.setattr('app.runtime.os.replace',locked)
    with ThreadPoolExecutor(max_workers=8) as executor:list(executor.map(lambda _:guard.check(),range(16)))
    assert len(attempts)==2
    value=json.loads((tmp_path/'.local-service/web.heartbeat').read_text(encoding='utf-8'))
    assert value['pid']==os.getpid() and not list((tmp_path/'.local-service').glob('*.tmp'))


def test_persistent_busy_knowledge_and_question_queues_do_not_starve_analysis(tmp_path,monkeypatch):
    from app.analysis_worker import AnalysisWorker
    from tests.test_question_jobs import question_fixture
    original=question_fixture(tmp_path);worker=AnalysisWorker(original.settings,repository=original.repository,model=original.model)
    processed=[]
    monkeypatch.setattr(worker.ingestion,'run_once',lambda:None)
    monkeypatch.setattr(worker.chat_submissions,'run_once',lambda:None)
    def knowledge():processed.append('knowledge');return True
    def question():processed.append('question');return True
    monkeypatch.setattr(worker.knowledge_worker,'run_once',knowledge)
    monkeypatch.setattr(worker.question_worker,'run_once',question)
    monkeypatch.setattr(worker.repository,'claim_next_analysis_job',lambda *a,**kw:object())
    monkeypatch.setattr(worker,'process_job',lambda job:processed.append('analysis'))
    try:assert worker.run_forever(max_tasks=12)==12
    finally:worker.runtime.release_lifecycle()
    assert all(processed.count(kind)>=2 for kind in ('knowledge','question','analysis'))
