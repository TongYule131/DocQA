"""停机清理已到期/移除附件：保留调用账本与请求键墓碑，不保留附件正文。"""
import argparse
import json
import re
import shutil
from pathlib import Path

from app.conversations import stop_conversation_jobs
from app.repository import Repository, now_iso
from app.runtime import process_start_token


def require_stopped(data):
    # 不杀进程；任一角色仍存活或记录损坏时拒绝物理清理。
    for path in (data / '.local-service').glob('*.ready'):
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        if process_start_token(record.get('pid')):
            raise RuntimeError('请先停止该数据目录的Web及全部worker，再执行附件清理')


def purge_expired(data):
    data = Path(data).resolve()
    require_stopped(data)
    repository = Repository(data / 'docqa.db')
    with repository.connect() as db:
        rows = db.execute('SELECT * FROM conversation_attachments WHERE purged_at IS NULL '
                          'AND (expires_at<=? OR revoked_at IS NOT NULL)', (now_iso(),)).fetchall()
    purged = []
    for row in rows:
        doc, conv = row['document_id'], row['conversation_id']
        if not re.fullmatch(r'[a-f0-9]{32}', doc):
            raise RuntimeError('临时原件标识不合法，停止清理')
        with repository.connect() as db:
            versions = [r[0] for r in db.execute('SELECT id FROM parse_versions WHERE document_id=?', (doc,))]
            tasks = [r[0] for r in db.execute('SELECT id FROM parse_tasks WHERE document_id=?', (doc,))]
            if db.execute('SELECT COUNT(*) FROM documents WHERE substr(id,1,12)=?', (doc[:12],)).fetchone()[0] != 1:
                raise RuntimeError('解析目录前缀存在歧义，请人工核对')
        targets = [data / 'uploads' / doc]
        results = data / 'parse-results'
        targets.extend(results / v for v in versions)
        targets.extend(results / ('failed-' + t) for t in tasks)
        # 包括写文件成功但尚未发布、以及崩溃留下的本次文档临时目录。
        targets.extend(results.glob('v-' + doc[:12] + '-*'))
        targets.extend(results.glob('.v-' + doc[:12] + '-*'))
        for target in set(targets):
            resolved = target.resolve()
            allowed = (resolved.parent == (data / 'uploads').resolve() and resolved.name == doc) or (
                resolved.parent == results.resolve() and resolved.name not in ('.', '..'))
            if not allowed or target.is_symlink():
                raise RuntimeError('清理路径越界，停止操作')
        # 文件删除可能被Windows占用阻止；保留未清理状态，下次明确维护时可继续。
        for target in set(targets):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        with repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            stop_conversation_jobs(db, conv)
            for table in ('question_results', 'question_checkpoints'):
                db.execute(f'DELETE FROM {table} WHERE job_id IN (SELECT id FROM question_jobs WHERE document_id=?)', (doc,))
            db.execute("UPDATE question_jobs SET snapshot_json='{}',question='[临时资料已清理]',result_id=NULL,stage_detail=NULL,error_message=NULL WHERE document_id=?", (doc,))
            db.execute("UPDATE conversation_turns SET question='[临时资料已清理]' WHERE conversation_id=?", (conv,))
            db.execute("UPDATE conversations SET memory_notes='',memory_epoch=memory_epoch+1,title='已清理的临时会话' WHERE id=?", (conv,))
            for table in ('embedding_vectors', 'embedding_vectors_v2', 'quality_warnings', 'chunks', 'sources', 'blocks'):
                db.execute(f'DELETE FROM {table} WHERE document_id=?', (doc,))
            db.execute("UPDATE parse_tasks SET status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,"
                       "cancel_requested=1,lease_token=NULL,lease_expires_at=NULL,error_message=NULL WHERE document_id=?", (doc,))
            db.execute("UPDATE parse_attempts SET detail=NULL,error_message=NULL WHERE task_id IN (SELECT id FROM parse_tasks WHERE document_id=?)", (doc,))
            db.execute("UPDATE parse_versions SET result_json_path=NULL,markdown_path=NULL,quality_summary=NULL WHERE document_id=?", (doc,))
            db.execute("UPDATE documents SET filename='已清理临时资料',error=NULL,chunk_count=0,active_parse_version_id=NULL,active_index_id=NULL WHERE id=?", (doc,))
            db.execute('UPDATE conversation_attachments SET purged_at=? WHERE document_id=?', (now_iso(), doc))
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('附件清理后外键检查未通过')
        purged.append(doc)
    temporary=purge_temporary_bases(data,repository)
    return {'purged': purged, 'temporary_bases':temporary,'provider_calls': 0}


def purge_temporary_bases(data,repository):
    """同一停机维护覆盖新临时库；只清内容，保留任务、费用及幂等墓碑。"""
    with repository.connect() as db:
        bases=db.execute("SELECT k.id FROM knowledge_bases k WHERE k.kind='temporary' AND k.purged_at IS NULL AND (k.expires_at<=? OR k.revoked_at IS NOT NULL OR EXISTS(SELECT 1 FROM knowledge_base_documents d JOIN conversation_attachments a ON a.document_id=d.document_id JOIN conversations c ON c.id=a.conversation_id WHERE d.knowledge_base_id=k.id AND (a.revoked_at IS NOT NULL OR a.purged_at IS NOT NULL OR c.deleted_at IS NOT NULL)))",(now_iso(),)).fetchall()
    purged=[]
    for (base_id,) in bases:
        with repository.connect() as db:
            docs={r[0] for r in db.execute('SELECT document_id FROM knowledge_base_documents WHERE knowledge_base_id=? UNION SELECT document_id FROM ingestion_jobs WHERE base_id=?',(base_id,base_id))}
            targets=[]
            for doc in docs:
                if not re.fullmatch(r'[a-f0-9]{32}',doc):raise RuntimeError('临时原件标识不合法，停止清理')
                if db.execute('SELECT COUNT(*) FROM documents WHERE substr(id,1,12)=?',(doc[:12],)).fetchone()[0]!=1:raise RuntimeError('临时解析目录前缀存在歧义')
                targets.append(data/'uploads'/doc)
                targets.extend(data/'parse-results'/r[0] for r in db.execute('SELECT id FROM parse_versions WHERE document_id=?',(doc,)))
                targets.extend(data/'parse-results'/('failed-'+r[0]) for r in db.execute('SELECT id FROM parse_tasks WHERE document_id=?',(doc,)))
                targets.extend((data/'parse-results').glob('v-'+doc[:12]+'-*'));targets.extend((data/'parse-results').glob('.v-'+doc[:12]+'-*'))
        for target in set(targets):
            resolved=target.resolve()
            if target.is_symlink() or resolved.parent not in ((data/'uploads').resolve(),(data/'parse-results').resolve()):raise RuntimeError('临时库清理路径越界')
        for target in set(targets):
            if target.is_dir():shutil.rmtree(target)
            else:target.unlink(missing_ok=True)
        with repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # 外部在途请求的账本不删除，也不把它误记为未收费。
            db.execute("UPDATE kb_task_calls SET status='uncertain',error_code='temporary_purged',finished_at=? WHERE task_id IN (SELECT id FROM kb_tasks WHERE base_id=?) AND status='intent'",(now_iso(),base_id))
            db.execute('DELETE FROM answer_stream_events WHERE kb_task_id IN (SELECT id FROM kb_tasks WHERE base_id=?)',(base_id,))
            db.execute('DELETE FROM kb_task_checkpoints WHERE task_id IN (SELECT id FROM kb_tasks WHERE base_id=?)',(base_id,))
            db.execute("UPDATE kb_tasks SET input_json='{}',result_json=NULL,error_message=NULL,cancel_requested=1,status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,lease_token=NULL,lease_expires_at=NULL WHERE base_id=?",(base_id,))
            db.execute("UPDATE kb_conversations SET title='已清理的临时会话',memory_notes='',initial_context_json='[]',memory_epoch=memory_epoch+1 WHERE base_id=?",(base_id,))
            db.execute("UPDATE chat_submissions SET question='[临时资料已清理]',status=CASE WHEN status='waiting' THEN 'cancelled' ELSE status END,error_message=NULL WHERE conversation_id IN (SELECT id FROM kb_conversations WHERE base_id=?)",(base_id,))
            db.execute("UPDATE ingestion_jobs SET status=CASE WHEN status IN ('queued','running','review') THEN 'cancelled' ELSE status END,error_message=NULL WHERE base_id=?",(base_id,))
            for doc in docs:
                # 历史附件迁移形成的新库也要清除其原入口的正文。
                db.execute('DELETE FROM answer_stream_events WHERE question_job_id IN (SELECT id FROM question_jobs WHERE document_id=?)',(doc,))
                for table in ('question_results','question_checkpoints'):db.execute(f'DELETE FROM {table} WHERE job_id IN (SELECT id FROM question_jobs WHERE document_id=?)',(doc,))
                db.execute("UPDATE question_jobs SET snapshot_json='{}',question='[临时资料已清理]',result_id=NULL WHERE document_id=?",(doc,))
                for table in ('embedding_vectors','embedding_vectors_v2','quality_warnings','chunks','sources','blocks'):db.execute(f'DELETE FROM {table} WHERE document_id=?',(doc,))
                db.execute("UPDATE parse_tasks SET cancel_requested=1,status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,request_summary='{}',error_message=NULL WHERE document_id=?",(doc,))
                db.execute('UPDATE parse_attempts SET detail=NULL,error_message=NULL WHERE task_id IN (SELECT id FROM parse_tasks WHERE document_id=?)',(doc,))
                db.execute('UPDATE parse_versions SET result_json_path=NULL,markdown_path=NULL,quality_summary=NULL WHERE document_id=?',(doc,))
                db.execute("UPDATE documents SET filename='已清理临时资料',active_parse_version_id=NULL,active_index_id=NULL,chunk_count=0,error=NULL WHERE id=?",(doc,))
            db.execute('UPDATE knowledge_bases SET purged_at=? WHERE id=?',(now_iso(),base_id))
            if db.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('临时库清理后外键检查未通过')
        purged.append(base_id)
    return purged


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(purge_expired(args.data_dir), ensure_ascii=False))
