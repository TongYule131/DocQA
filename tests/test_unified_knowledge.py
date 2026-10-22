"""统一上传与会话的业务回归；真实SQLite/解析/校验链路，供应商使用合成桩。"""
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import pytest
from fastapi import HTTPException,UploadFile
from app import migrations
from app.ingestion import Ingestion,IngestionCoordinator
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from app.product_access import ProductAccess,actor_context
from app.parse_worker import ParseWorker
from app.conversations import Conversations
from app.conversation_routes import ConversationUpdate
from app.unified_chat import UnifiedChat,ChatSubmissionCoordinator
from app.repository import now_iso
from tests.test_question_jobs import question_fixture,LocalRuntime,TEXT,QUESTION
from tests.test_product_access import team,publish


def context(tmp_path,policy='auto'):
    env=question_fixture(tmp_path);access=ProductAccess(env.repository,env.settings,env.rag.index)
    base=access.create_base('自动入库','合成验收',activation_policy=policy)
    questions=KnowledgeQuestions(access,env.rag);legacy=Conversations(access,env.service)
    chat=UnifiedChat(access,questions,legacy)
    return env,access,base,questions,chat


def upload(access,base,key='upload',text=TEXT,replace_id=None):
    return Ingestion(access).submit(base['id'],UploadFile(filename='合成资料.txt',file=io.BytesIO(text.encode())),key,replace_document_id=replace_id)


def prepare(env,access,job,*,activate=True):
    coordinator=IngestionCoordinator(env.settings,env.repository,embedding=env.embedding)
    coordinator.run_once(job['id'])
    row=Ingestion(access).require(job['id'],write=True)
    worker=ParseWorker(env.settings,env.repository);worker.runtime=LocalRuntime()
    if row['parse_task_id']:worker.run_task(row['parse_task_id'])
    coordinator.run_once(job['id']);row=Ingestion(access).require(job['id'],write=True)
    assert row['index_task_id'],row
    KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model).run_once(row['index_task_id'])
    coordinator.run_once(job['id'])
    row=Ingestion(access).require(job['id'],write=True)
    assert row['status']==('succeeded' if activate else 'review'),row
    return row


def test_upload_automatically_reaches_ready_and_waiting_question_runs_once(tmp_path):
    env,access,base,questions,chat=context(tmp_path)
    job=upload(access,base);conv=chat.create(base['id'],'create',[job['document_id']])
    request=chat.submit(conv['id'],QUESTION,'turn')
    assert request['status']=='waiting' and not env.embedding.calls
    prepare(env,access,job)
    coordinator=ChatSubmissionCoordinator(env.settings,env.repository,knowledge=questions)
    coordinator.run_once();coordinator.run_once()
    detail=chat.detail(conv['id']);assert len(detail['turns'])==1
    task=detail['turns'][0];KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model).run_once(task['id'])
    detail=chat.detail(conv['id']);assert detail['turns'][0]['status']=='succeeded',detail
    assert len(env.embedding.calls)==2 and len(env.model.calls)==1
    assert chat.submit(conv['id'],QUESTION,'turn')['id']==request['id']
    for _ in range(3):chat.detail(conv['id']);chat.scopes();chat.list()
    assert len(env.embedding.calls)==2 and len(env.model.calls)==1
    assert {c['document_id'] for c in detail['turns'][0]['result']['citations']}=={job['document_id']}


def test_same_content_dedup_and_same_key_conflict_are_atomic(tmp_path):
    env,access,base,_,_=context(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:jobs=list(pool.map(lambda key:upload(access,base,key),['one','two']))
    assert jobs[0]['id']==jobs[1]['id']
    assert len(list((env.settings.data_dir/'uploads').iterdir()))==1
    assert upload(access,base,'one')['id']==jobs[0]['id']
    with pytest.raises(HTTPException,match='409'):upload(access,base,'one','其他文件')
    assert not env.embedding.calls


def test_review_blocks_only_selected_new_file_and_old_ready_scope_continues(tmp_path):
    env,access,base,questions,chat=context(tmp_path,'review')
    first=upload(access,base);prepare(env,access,first,activate=False)
    conv=chat.create(base['id'],'create',[first['document_id']]);pending=chat.submit(conv['id'],QUESTION,'turn')
    ChatSubmissionCoordinator(env.settings,env.repository,knowledge=questions).run_once()
    assert not chat.detail(conv['id'])['turns']
    Ingestion(access).activate(first['id'])
    ChatSubmissionCoordinator(env.settings,env.repository,knowledge=questions).run_once()
    assert chat.detail(conv['id'])['submissions'][0]['status']=='submitted'
    second=upload(access,base,'second','第二份内容。')
    Ingestion(access).cancel(second['id'])
    other=chat.create(base['id'],'other',[])
    assert chat.submit(other['id'],QUESTION,'turn')['status']=='submitted'
    bad=chat.create(base['id'],'bad',[second['document_id']])
    assert chat.submit(bad['id'],QUESTION,'turn')['status']=='failed'
    assert not chat.detail(bad['id'])['turns']


def test_waiting_submission_memory_change_and_archive_do_not_generate(tmp_path):
    env,access,base,_,chat=context(tmp_path);job=upload(access,base)
    conv=chat.create(base['id'],'create',[job['document_id']]);chat.submit(conv['id'],QUESTION,'turn')
    conv=chat.update(conv['id'],ConversationUpdate(expected_revision=0,memory_enabled=False))
    assert conv['submissions'][0]['status']=='cancelled'
    chat.submit(conv['id'],QUESTION,'two')
    conv=chat.update(conv['id'],ConversationUpdate(expected_revision=1,archived=True))
    assert all(s['status']=='cancelled' for s in conv['submissions'])
    assert not env.embedding.calls


def test_replacement_creates_snapshot_and_preserves_original_citations(tmp_path):
    env,access,base,questions,chat=context(tmp_path)
    first=upload(access,base);prepare(env,access,first)
    snapshot=access.base(base['id'])['active_snapshot_id']
    next_job=upload(access,base,'replace',TEXT+'新版说明。',first['document_id']);prepare(env,access,next_job)
    assert access.base(base['id'])['active_snapshot_id']!=snapshot
    with env.repository.connect() as db:
        assert db.execute('SELECT document_id FROM knowledge_base_snapshot_documents WHERE snapshot_id=?',(snapshot,)).fetchone()[0]==first['document_id']
    assert questions.base_scope(base['id'],[first['document_id']])[1][0]['document_id']==next_job['document_id']


def test_uncertain_index_never_auto_retries_and_retry_retains_budget(tmp_path):
    env,access,base,_,_=context(tmp_path);job=upload(access,base);co=IngestionCoordinator(env.settings,env.repository,embedding=env.embedding)
    co.run_once();row=Ingestion(access).require(job['id'],write=True);parse=ParseWorker(env.settings,env.repository);parse.runtime=LocalRuntime();parse.run_task(row['parse_task_id']);co.run_once()
    original=env.embedding.embed
    def lost(_):raise OSError('synthetic connection lost')
    env.embedding.embed=lost
    row=Ingestion(access).require(job['id'],write=True);worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model);worker.run_once(row['index_task_id']);co.run_once()
    assert Ingestion(access).require(job['id'])['status']=='needs_attention'
    for _ in range(3):co.run_once();worker.run_once()
    with pytest.raises(HTTPException):Ingestion(access).retry(job['id'])
    Ingestion(access).retry(job['id'],True);env.embedding.embed=original;worker.run_once(row['index_task_id']);co.run_once()
    assert Ingestion(access).require(job['id'])['status']=='succeeded'
    with env.repository.connect() as db:assert tuple(db.execute('SELECT requests_used,max_requests FROM kb_tasks WHERE id=?',(row['index_task_id'],)).fetchone())==(2,2)


def test_changed_config_or_original_stops_before_provider_request(tmp_path):
    env,access,base,_,_=context(tmp_path);job=upload(access,base)
    (env.settings.data_dir/'uploads'/job['document_id']).write_bytes(b'changed')
    co=IngestionCoordinator(env.settings,env.repository,embedding=env.embedding);co.run_once()
    row=Ingestion(access).require(job['id']);parse=ParseWorker(env.settings,env.repository);parse.runtime=LocalRuntime();parse.run_task(row['parse_task_id']);co.run_once()
    assert Ingestion(access).require(job['id'])['status']=='failed' and not env.embedding.calls
    another=upload(access,base,'another','另一份资料。')
    with env.repository.connect() as db:db.execute('UPDATE knowledge_bases SET chunk_max_chars=900 WHERE id=?',(base['id'],))
    co.run_once(another['id']);assert Ingestion(access).require(another['id'])['status']=='failed'


def test_legacy_history_is_not_duplicated_or_regenerated(tmp_path):
    env,access,base,questions,chat=context(tmp_path);access.bind_document(base['id'],'qdoc');access.publish(base['id'],0)
    conv=chat.legacy.create('qdoc','old')
    env.service.submit('qdoc',{'question':QUESTION,'idempotency_key':'independent'})
    assert len(chat.list())==2
    old='d:'+conv['id'];detail=chat.detail(old);assert detail['turns']==[]
    request=chat.submit(old,QUESTION,'new')
    assert request['status']=='submitted';assert len(chat.list())==2
    assert len(chat.detail(old)['turns'])==1
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM question_jobs').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM question_results').fetchone()[0]==0
    assert not env.embedding.calls and not env.model.calls


def test_temporary_kb_is_private_to_customer_including_admin_and_expiry(team):
    env,users,clients=team;alice,bob,admin=(clients[n] for n in ['alice','bob','admin'])
    base=alice.post('/api/chat/temporary',json={'request_key':'temp'}).json()
    assert alice.post('/api/chat/temporary',json={'request_key':'temp'}).json()['id']==base['id']
    response=alice.post('/api/ingestions',data={'base_id':base['id']},files={'file':('private.txt',TEXT.encode(),'text/plain')},headers={'Idempotency-Key':'one'})
    assert response.status_code==202,response.text
    job=response.json()
    for client in (bob,admin):
        assert client.get('/api/ingestions/'+job['id']).status_code==404
        assert client.get('/api/documents/'+job['document_id']+'/original').status_code==404
        assert all(b['id']!=base['id'] for b in client.get('/api/chat/scopes').json())
    assert all(b['id']!=base['id'] for b in admin.get('/api/admin/overview').json()['bases'])
    team_base=publish(clients,users)
    assert alice.post('/api/ingestions',data={'base_id':team_base['id']},files={'file':('no.txt',b'no','text/plain')},headers={'Idempotency-Key':'denied'}).status_code==403
    with env.repository.connect() as db:db.execute("UPDATE knowledge_bases SET expires_at='2000-01-01' WHERE id=?",(base['id'],))
    assert alice.get('/api/documents/'+job['document_id']+'/original').status_code==404
    assert alice.get('/api/ingestions/'+job['id']).status_code==404
    IngestionCoordinator(env.settings,env.repository,embedding=env.embedding).run_once()
    with env.repository.connect() as db:assert db.execute('SELECT status FROM ingestion_jobs WHERE id=?',(job['id'],)).fetchone()[0]=='cancelled'


def test_v14_migration_is_atomic_private_and_reentrant(tmp_path):
    path=tmp_path/'migrate.db'
    with sqlite3.connect(path,isolation_level=None) as db:
        db.row_factory=sqlite3.Row;db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        for version in range(1,14):migrations.MIGRATIONS[version](db);db.execute('INSERT INTO schema_migrations VALUES (?,?)',(version,now_iso()))
        db.execute("INSERT INTO documents(id,filename,size,created_at,status) VALUES ('keep','legacy.txt',1,'synthetic','uploaded')")
        original=tuple(db.execute('SELECT * FROM documents').fetchone())
        migrations.MigrationHook.register(14,lambda:(_ for _ in ()).throw(RuntimeError('synthetic rollback')))
        try:
            with pytest.raises(migrations.MigrationError):migrations.run_migrations(db)
        finally:migrations.MigrationHook.clear()
        assert migrations.detect_version(db)==13
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='ingestion_jobs'").fetchone() is None
        migrations.run_migrations(db);assert tuple(db.execute('SELECT * FROM documents').fetchone())==original
        assert db.execute('SELECT kind FROM knowledge_bases').fetchone()[0]=='legacy'
        assert not db.execute('SELECT 1 FROM knowledge_base_members').fetchone()
        assert migrations.run_migrations(db)['applied']==[]


def test_waiting_cancel_race_stops_before_task_creation(tmp_path,monkeypatch):
    env,access,base,questions,chat=context(tmp_path);job=upload(access,base)
    conv=chat.create(base['id'],'create',[job['document_id']]);request=chat.submit(conv['id'],QUESTION,'turn');prepare(env,access,job)
    original=questions.submit
    def cancel_then_submit(*args,**kwargs):
        with env.repository.connect() as db:db.execute("UPDATE chat_submissions SET status='cancelled' WHERE id=?",(request['id'],))
        return original(*args,**kwargs)
    monkeypatch.setattr(questions,'submit',cancel_then_submit)
    ChatSubmissionCoordinator(env.settings,env.repository,knowledge=questions).run_once()
    assert not chat.detail(conv['id'])['turns']
    assert not env.model.calls


def test_coordinator_crash_after_task_creation_reuses_same_task(tmp_path,monkeypatch):
    env,access,base,questions,chat=context(tmp_path);job=upload(access,base)
    conv=chat.create(base['id'],'create',[job['document_id']]);chat.submit(conv['id'],QUESTION,'turn');prepare(env,access,job)
    original=questions.submit
    def crashed(*args,**kwargs):original(*args,**kwargs);raise SystemExit('synthetic worker crash')
    monkeypatch.setattr(questions,'submit',crashed)
    co=ChatSubmissionCoordinator(env.settings,env.repository,knowledge=questions)
    with pytest.raises(SystemExit):co.run_once()
    monkeypatch.setattr(questions,'submit',original);co.run_once()
    detail=chat.detail(conv['id']);assert len(detail['turns'])==1 and detail['submissions'][0]['status']=='submitted'
    assert not env.model.calls


def test_temporary_cleanup_retains_accounting_and_request_tombstone(tmp_path):
    from app.attachment_maintenance import purge_expired
    env,access,_,questions,chat=context(tmp_path)
    base=chat.temporary('temp');job=upload(access,base);prepare(env,access,job)
    with env.repository.connect() as db:db.execute("UPDATE knowledge_bases SET revoked_at=? WHERE id=?",(now_iso(),base['id']))
    result=purge_expired(env.settings.data_dir)
    assert base['id'] in result['temporary_bases']
    assert not (env.settings.data_dir/'uploads'/job['document_id']).exists()
    with env.repository.connect() as db:
        assert db.execute('SELECT requests_used FROM kb_tasks WHERE base_id=?',(base['id'],)).fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM kb_task_calls').fetchone()[0]==1
        assert not db.execute('SELECT 1 FROM chunks WHERE document_id=?',(job['document_id'],)).fetchone()
    with pytest.raises(HTTPException):chat.temporary('temp')


def test_memory_conditions_help_retrieval_but_are_not_evidence(tmp_path):
    env,access,base,questions,chat=context(tmp_path);job=upload(access,base);prepare(env,access,job)
    conv=chat.create(base['id'],'create',[]);chat.update(conv['id'],ConversationUpdate(expected_revision=0,memory_notes='保留全书的部分数量'))
    request=chat.submit(conv['id'],QUESTION,'turn')
    task=questions.tasks.row(request['task_id']);payload=json.loads(task['input_json'])
    assert '保留全书的部分数量' in payload['retrieval_question']
    KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model).run_once(task['id'])
    prompt=json.loads(env.model.calls[0][1]);assert prompt['conversation_context']['notes']=='保留全书的部分数量'
    assert all('保留全书的部分数量' not in json.dumps(r,ensure_ascii=False) for r in prompt['references'])


def test_concurrent_waiting_submission_reuses_one_record(tmp_path):
    env,access,base,_,chat=context(tmp_path);job=upload(access,base);conv=chat.create(base['id'],'create',[job['document_id']])
    with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(lambda _:chat.submit(conv['id'],QUESTION,'turn'),range(2)))
    assert rows[0]['id']==rows[1]['id']
    assert len(chat.detail(conv['id'])['submissions'])==1 and not env.model.calls


def test_migrated_legacy_attachment_respects_new_kb_revocation(team):
    from app.attachment_schema import attachment_available
    env,users,clients=team;alice=clients['alice']
    old=alice.post('/api/conversations/temporary',files={'file':('legacy.txt',TEXT.encode(),'text/plain')},headers={'Idempotency-Key':'old-upload'}).json()
    base=alice.post('/api/chat/temporary',json={'request_key':'migration-scope'}).json()
    with env.repository.connect() as db:
        db.execute('INSERT INTO knowledge_base_documents VALUES (?,?,?)',(base['id'],old['document_id'],now_iso()))
        assert attachment_available(db,old['document_id'])
    assert alice.delete('/api/chat/temporary/'+base['id']).status_code==200
    assert alice.get('/api/documents/'+old['document_id']+'/original').status_code==410
    with env.repository.connect() as db:assert not attachment_available(db,old['document_id'])
    with env.repository.connect() as db:db.execute('UPDATE knowledge_bases SET revoked_at=NULL WHERE id=?',(base['id'],))
    assert alice.delete('/api/conversations/'+old['id']+'/attachment').status_code==200
    assert alice.get('/api/knowledge-bases/'+base['id']).status_code==404


def test_existing_ingestion_alias_concurrent_same_key_is_idempotent(tmp_path):
    _,access,base,_,_=context(tmp_path);first=upload(access,base)
    with ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(lambda _:upload(access,base,'same-alias'),range(4)))
    assert {r['id'] for r in rows}=={first['id']}


def test_old_attachment_purge_clears_migrated_kb_content_too(tmp_path):
    from app.attachment_maintenance import purge_expired
    env,access,_,questions,chat=context(tmp_path);base=chat.temporary('private');job=upload(access,base);prepare(env,access,job)
    conv=chat.create(base['id'],'create',[]);request=chat.submit(conv['id'],QUESTION,'turn')
    with env.repository.connect() as db:
        db.execute("INSERT INTO conversations(id,document_id,title,request_key,created_at,updated_at) VALUES ('legacy',?,'private','old',?,?)",(job['document_id'],now_iso(),now_iso()))
        db.execute("INSERT INTO conversation_attachments VALUES (?,'legacy',NULL,'9999-01-01',?,NULL,?)",(job['document_id'],now_iso(),now_iso()))
    result=purge_expired(env.settings.data_dir)
    assert base['id'] in result['temporary_bases']
    with env.repository.connect() as db:
        assert db.execute('SELECT input_json FROM kb_tasks WHERE id=?',(request['task_id'],)).fetchone()[0]=='{}'
        assert db.execute('SELECT purged_at FROM knowledge_bases WHERE id=?',(base['id'],)).fetchone()[0]
