"""临时资料清理的内容边界；费用墓碑、普通资料和原始失败不被改写。"""
import json
import os
import subprocess
import pytest
from app.repository import Repository
from app import migrations
from app.attachment_maintenance import purge_expired
from app.repository import now_iso
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from app.unified_chat import UnifiedChat
from app.conversations import Conversations
from app.clarifications import resolve
from tests.test_knowledge_chain import library
from tests.test_question_jobs import LocalRuntime,REPLY,QUESTION
from tests.test_product_access import team
from tests.test_attachments import ready,expire


def test_temporary_base_purge_scrubs_actual_confirmed_clarification(tmp_path):
    env,access,base=library(tmp_path);q=KnowledgeQuestions(access,env.rag);conv=q.create(base['id'],'new')
    task=q.submit(conv['id'],'两者有什么区别','question')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(task['id'])
    chat=UnifiedChat(access,q,Conversations(access,env.service));clar='clarify-'+task['id']
    resolve(chat,'k:'+conv['id'],clar,['私有对象甲','私有对象乙'],'confirm')
    with env.repository.connect() as db:
        assert '私有对象甲' in db.execute('SELECT choices_json FROM conversation_clarifications WHERE id=?',(clar,)).fetchone()[0]
        # 使用真实已发布库创建澄清；其文档按独立普通资料保留，模拟临时库已解除文档关联。
        db.execute('DELETE FROM knowledge_base_documents WHERE knowledge_base_id=?',(base['id'],))
        db.execute("UPDATE knowledge_bases SET kind='temporary',expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(base['id'],))
        calls=[tuple(r) for r in db.execute('SELECT * FROM kb_task_calls')]
    assert purge_expired(tmp_path)['temporary_bases']==[base['id']]
    with env.repository.connect() as db:
        row=db.execute('SELECT state,choices_json,resolve_key FROM conversation_clarifications WHERE id=?',(clar,)).fetchone()
        assert row['state']=='cancelled' and row['choices_json']=='[]' and row['resolve_key']=='confirm'
        assert calls==[tuple(r) for r in db.execute('SELECT * FROM kb_task_calls')]
    assert env.repository.chunks('qdoc') and not env.embedding.calls and not env.model.calls


def test_legacy_attachment_purge_scrubs_stream_and_shared_feedback(team):
    env,_,clients=team;c,path=ready(team);alice=clients['alice']
    class StreamModel:
        def generate_stream(self,system,user,on_text,check_active):
            check_active();on_text(REPLY);return REPLY
    env.rag.model=StreamModel()
    response=alice.post(path+'/turns',json={'question':QUESTION,'idempotency_key':'stream'})
    job=response.json()['job']
    from app.question_worker import QuestionWorker
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    assert env.service.get_job(job['id'])['status']=='succeeded'
    feedback=alice.post('/api/feedback',json={'source':'question','task_id':job['id'],'request_key':'feedback',
        'category':'expression','comment':'私有反馈内容','share_context':True})
    assert feedback.status_code==201
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM answer_stream_events WHERE question_job_id=?',(job['id'],)).fetchone()[0]>0
        ledger=[tuple(r) for r in db.execute('SELECT * FROM question_calls')]
    expire(env,c['document_id'])
    for client in clients.values():client.__exit__(None,None,None)
    # TestClient与pytest共用PID；关闭生命周期后移去本隔离环境保留的ready文件。
    for marker in (env.settings.data_dir/'.local-service').glob('*.ready'):marker.unlink()
    purge_expired(env.settings.data_dir)
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM answer_stream_events WHERE question_job_id=?',(job['id'],)).fetchone()[0]==0
        row=db.execute('SELECT context_json,comment,share_context FROM answer_feedback WHERE id=?',(feedback.json()['id'],)).fetchone()
        assert row['context_json'] is None and row['comment']=='' and row['share_context']==0
        assert ledger==[tuple(r) for r in db.execute('SELECT * FROM question_calls')]
    assert env.repository.chunks('qdoc')


def test_purge_rejects_old_schema_before_content_or_database_changes(tmp_path,monkeypatch):
    repo=Repository(tmp_path/'docqa.db')
    with monkeypatch.context() as historical:
        historical.setattr(migrations,'SCHEMA_VERSION',14)
        repo.initialize()
    asset=tmp_path/'uploads'/'original';asset.parent.mkdir(exist_ok=True);asset.write_bytes(b'keep')
    with pytest.raises(migrations.MigrationError):purge_expired(tmp_path)
    assert asset.read_bytes()==b'keep'
    with repo.connect() as db:assert migrations.detect_version(db)==14


def test_purge_missing_directory_does_not_create_a_database(tmp_path):
    missing=tmp_path/'missing'
    with pytest.raises((RuntimeError,ValueError)):purge_expired(missing)
    assert not missing.exists()


def test_purge_rejects_aliased_upload_root_before_deleting_another_directory(tmp_path):
    data=tmp_path/'data';data.mkdir();outside=tmp_path/'outside';outside.mkdir()
    repo=Repository(data/'docqa.db');repo.initialize();uploads=data/'uploads'
    if os.name=='nt':
        result=subprocess.run(['cmd.exe','/c','mklink','/J',str(uploads),str(outside)],capture_output=True)
        assert result.returncode==0
    else:uploads.symlink_to(outside,target_is_directory=True)
    identifier='a'*32;asset=outside/identifier;asset.write_bytes(b'keep outside the selected data directory')
    assert asset.resolve().is_relative_to(tmp_path.resolve())
    from app.schemas import Document
    repo.create(Document(id=identifier,filename='temporary.txt',size=10,created_at=now_iso(),format='txt',status='uploaded'))
    with repo.connect() as db:
        db.execute('INSERT INTO conversations(id,document_id,title,request_key,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                   ('legacy',identifier,'temporary','create',now_iso(),now_iso()))
        db.execute('INSERT INTO conversation_attachments(document_id,conversation_id,expires_at,created_at) VALUES (?,?,?,?)',
                   (identifier,'legacy','2000-01-01T00:00:00+00:00',now_iso()))
    with pytest.raises(RuntimeError,match='链接|越界'):purge_expired(data)
    assert asset.read_bytes()==b'keep outside the selected data directory'
    with repo.connect() as db:assert db.execute('SELECT purged_at FROM conversation_attachments').fetchone()[0] is None
