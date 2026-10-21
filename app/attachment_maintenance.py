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
    return {'purged': purged, 'provider_calls': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(purge_expired(args.data_dir), ensure_ascii=False))
