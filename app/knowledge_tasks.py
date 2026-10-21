"""知识库任务：短事务领取、每个HTTP请求预扣、检查点恢复与真实执行节点。"""
from contextlib import contextmanager
from hashlib import sha256
import json
import time
from uuid import uuid4

from fastapi import HTTPException

from app.repository import now_iso, lease_expiry_iso
from app.question_jobs import _usage_metadata

KNOWLEDGE_PROMPT_VERSION = 'knowledge-task-v1'
KNOWLEDGE_RULE = '\n这是知识库多文档问答。不同资料的观点冲突时分别引用，不擅自调和。先用自然语言直答，再按问题需要解释；避免逐句重复“文档指出”。'


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return sha256(dump(value).encode()).hexdigest()


def configuration(settings, embedding):
    from app.rag_prompts import TASK_PROMPT_VERSION
    return fingerprint([embedding.signature, settings.deepseek_base_url, settings.deepseek_model,
        settings.deepseek_thinking, TASK_PROMPT_VERSION, KNOWLEDGE_PROMPT_VERSION, KNOWLEDGE_RULE, settings.question_context_max_chars,
        settings.question_input_max_chars, settings.question_context_k])


class TaskStopped(Exception):
    def __init__(self, code, message, status='failed'):
        self.code, self.message, self.status = code, message, status


class KnowledgeTasks:
    def __init__(self, repository):
        self.repository = repository

    def row(self, identifier):
        with self.repository.connect() as db:
            row = db.execute('SELECT * FROM kb_tasks WHERE id=?', (identifier,)).fetchone()
        return dict(row) if row else None

    def create(self, kind, payload, key, *, base_id=None, document_id=None, owner=None, max_requests=8, conversation_id=None):
        identifier, stamp, encoded = 'ktask-'+uuid4().hex, now_iso(), dump(payload)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM kb_tasks WHERE owner_user_id IS ? AND kind=? AND request_key=?', (owner,kind,key)).fetchone()
            if prior:
                if prior['input_hash']!=fingerprint(payload): raise HTTPException(409,'请求键已用于不同的输入')
                return dict(prior)
            if conversation_id:
                conv=db.execute('SELECT * FROM kb_conversations WHERE id=? AND owner_user_id IS ?', (conversation_id,owner)).fetchone()
                if not conv or conv['base_id']!=base_id or conv['memory_epoch']!=payload.get('memory_epoch'):
                    raise HTTPException(409,'会话范围或记忆已变更，请刷新后提交')
                if db.execute("SELECT 1 FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status IN ('queued','running')", (conversation_id,)).fetchone():
                    raise HTTPException(409,'本会话已有活动任务')
            if kind=='index' and db.execute("SELECT 1 FROM kb_tasks WHERE document_id=? AND kind='index' AND status IN ('queued','running')", (document_id,)).fetchone():
                raise HTTPException(409,'文档已有索引任务，请刷新查看进度')
            db.execute('INSERT INTO kb_tasks(id,kind,base_id,document_id,owner_user_id,request_key,input_json,input_hash,max_requests,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,kind,base_id,document_id,owner,key,encoded,fingerprint(payload),max_requests,stamp,stamp))
            if conversation_id:
                number=db.execute('SELECT COALESCE(MAX(turn_no),0)+1 FROM kb_conversation_turns WHERE conversation_id=?',(conversation_id,)).fetchone()[0]
                if number>100:raise HTTPException(409,'会话已达100轮，请新建会话')
                db.execute('INSERT INTO kb_conversation_turns VALUES (?,?,?)',(conversation_id,number,identifier))
                db.execute("UPDATE kb_conversations SET updated_at=?,title=CASE WHEN title='新会话' THEN ? ELSE title END WHERE id=?",(stamp,payload.get('question','')[:60],conversation_id))
        return self.row(identifier)

    @staticmethod
    def allowed(db, row):
        if row['owner_user_id']:
            actor=db.execute('SELECT role,active FROM product_users WHERE id=?',(row['owner_user_id'],)).fetchone()
            if not actor or not actor['active']:return False
            if row['kind']=='index' and actor['role']!='admin':return False
            if actor['role']!='admin' and not db.execute('SELECT 1 FROM knowledge_base_members WHERE knowledge_base_id=? AND user_id=?',(row['base_id'],row['owner_user_id'])).fetchone():return False
        if row['kind']=='question':
            payload=json.loads(row['input_json'])
            if payload.get('conversation_id') and row['status'] in ('queued','running'):
                conv=db.execute('SELECT memory_epoch FROM kb_conversations WHERE id=?',(payload['conversation_id'],)).fetchone()
                if not conv or conv[0]!=payload.get('memory_epoch'):return False
            base=db.execute('SELECT active_snapshot_id FROM knowledge_bases WHERE id=?',(row['base_id'],)).fetchone()
            if not base or not base[0]:return False
            current={r[0] for r in db.execute('SELECT document_id FROM knowledge_base_snapshot_documents WHERE snapshot_id=?',(base[0],))}
            if not {d['document_id'] for d in payload.get('documents',[])}<=current:return False
            if payload.get('tool_revision') is not None and row['status'] in ('queued','running'):
                tool=db.execute('SELECT enabled,revision FROM mcp_tools WHERE id=?',(payload['route'].get('tool_id'),)).fetchone()
                if not tool or not tool['enabled'] or tool['revision']!=payload['tool_revision']:return False
        return True

    @classmethod
    def valid(cls, db, identifier, token):
        row=db.execute('SELECT * FROM kb_tasks WHERE id=?',(identifier,)).fetchone()
        return bool(row and row['status']=='running' and row['lease_token']==token and row['lease_expires_at']>now_iso()
                    and not row['cancel_requested'] and cls.allowed(db,row))

    def claim(self, worker, seconds, identifier=None):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            expired=db.execute("SELECT id FROM kb_tasks WHERE status='running' AND lease_expires_at<=?",(now_iso(),)).fetchall()
            for entry in expired:
                pending=db.execute("SELECT 1 FROM kb_task_calls WHERE task_id=? AND status='intent'",(entry[0],)).fetchone()
                db.execute("UPDATE kb_task_calls SET status='uncertain',error_code='call_uncertain',finished_at=? WHERE task_id=? AND status='intent'",(now_iso(),entry[0]))
                db.execute("UPDATE kb_trace_spans SET status='interrupted',finished_at=?,error_code='lease_expired' WHERE task_id=? AND status='running'",(now_iso(),entry[0]))
                db.execute('UPDATE kb_tasks SET status=?,error_code=?,error_message=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?',
                    ('needs_attention' if pending else 'queued','call_uncertain' if pending else None,'存在未确认调用，请核实后明确重试' if pending else None,entry[0]))
            sql="SELECT * FROM kb_tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
            row=db.execute(sql if identifier is None else "SELECT * FROM kb_tasks WHERE status='queued' AND id=?",() if identifier is None else (identifier,)).fetchone()
            if not row:return None
            if not self.allowed(db,row):
                db.execute("UPDATE kb_tasks SET status='cancelled',error_code='access_revoked',error_message='账号、会话或资料权限已变更',finished_at=? WHERE id=?",(now_iso(),row['id']))
                return None
            token=worker+':'+uuid4().hex
            db.execute("UPDATE kb_tasks SET status='running',lease_token=?,lease_expires_at=?,attempt_count=attempt_count+1,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                (token,lease_expiry_iso(seconds),now_iso(),now_iso(),row['id']))
        return self.row(row['id'])

    def check(self, identifier, token):
        with self.repository.connect() as db:
            if not self.valid(db,identifier,token):raise TaskStopped('task_stopped','任务已停止、租约失效或权限已变化','cancelled')

    def renew(self, identifier, token, seconds):
        with self.repository.connect() as db:
            if not self.valid(db,identifier,token):return False
            return bool(db.execute('UPDATE kb_tasks SET lease_expires_at=? WHERE id=? AND lease_token=?',(lease_expiry_iso(seconds),identifier,token)).rowcount)

    def stage(self, identifier, token, stage):
        self.check(identifier,token)
        with self.repository.connect() as db:db.execute('UPDATE kb_tasks SET stage=?,updated_at=? WHERE id=? AND lease_token=?',(stage,now_iso(),identifier,token))

    @contextmanager
    def span(self, job, name, kind, parent=None):
        self.check(job['id'],job['lease_token'])
        identifier='span-'+uuid4().hex;start=time.perf_counter()
        with self.repository.connect() as db:
            db.execute("INSERT INTO kb_trace_spans(id,task_id,attempt_no,parent_id,name,kind,status,started_at) VALUES (?,?,?,?,?,?,'running',?)",
                (identifier,job['id'],job['attempt_count'],parent,name,kind,now_iso()))
        status,code='succeeded',None
        try:yield identifier
        except Exception as exc:
            status,code='failed',exc.code if isinstance(exc,TaskStopped) else 'step_failed'
            raise
        finally:
            with self.repository.connect() as db:db.execute('UPDATE kb_trace_spans SET status=?,finished_at=?,elapsed_ms=?,error_code=? WHERE id=?',(status,now_iso(),round((time.perf_counter()-start)*1000),code,identifier))

    def checkpoint(self, identifier, name):
        with self.repository.connect() as db:row=db.execute('SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name=?',(identifier,name)).fetchone()
        return json.loads(row[0]) if row else None

    def call(self, job, role, work, *, checkpoint=None, validate=lambda value:value):
        saved=self.checkpoint(job['id'],checkpoint) if checkpoint else None
        if saved is not None:return validate(saved)
        identifier,token=job['id'],job['lease_token']
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.valid(db,identifier,token):raise TaskStopped('task_stopped','任务已停止','cancelled')
            row=db.execute('SELECT * FROM kb_tasks WHERE id=?',(identifier,)).fetchone()
            if row['requests_used']>=row['max_requests']:raise TaskStopped('budget_exhausted','任务请求预算不足，未发起新的请求','needs_attention')
            sequence=row['requests_used']+1
            db.execute('UPDATE kb_tasks SET requests_used=? WHERE id=?',(sequence,identifier))
            db.execute("INSERT INTO kb_task_calls(id,task_id,sequence_no,role,status,started_at) VALUES (?,?,?,?,'intent',?)",('kcall-'+uuid4().hex,identifier,sequence,role,now_iso()))
        started=time.perf_counter()
        try:
            raw,usage=work()
            value=validate(raw)
            with self.repository.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # 调用完成即结算，即使中途撤权也保留费用记录，但不能发布正文。
                db.execute("UPDATE kb_task_calls SET status='succeeded',usage_json=?,finished_at=?,elapsed_ms=? WHERE task_id=? AND sequence_no=?",(dump(_usage_metadata(usage)),now_iso(),round((time.perf_counter()-started)*1000),identifier,sequence))
                if self.valid(db,identifier,token) and checkpoint:
                    db.execute('INSERT OR IGNORE INTO kb_task_checkpoints VALUES (?,?,?,?)',(identifier,checkpoint,dump(value),now_iso()))
            self.check(identifier,token)
            return value
        except Exception as exc:
            from app.rag_validation import ModelOutputError
            uncertain=(getattr(exc,'status_code',None)==504 or isinstance(exc,TaskStopped) and exc.status=='needs_attention'
                or not hasattr(exc,'status_code') and not isinstance(exc,(TaskStopped,ValueError,ModelOutputError)))
            with self.repository.connect() as db:
                db.execute("UPDATE kb_task_calls SET status=?,error_code=?,finished_at=?,elapsed_ms=? WHERE task_id=? AND sequence_no=? AND status='intent'",
                    ('uncertain' if uncertain else 'failed','call_uncertain' if uncertain else 'call_failed',now_iso(),round((time.perf_counter()-started)*1000),identifier,sequence))
            if isinstance(exc,TaskStopped):raise
            code='output_'+exc.reason if isinstance(exc,ModelOutputError) else 'call_uncertain' if uncertain else 'call_failed'
            raise TaskStopped(code,'调用结果不确定，请核实后明确重试' if uncertain else '调用或输出校验失败；未发布结果','needs_attention' if uncertain else 'failed') from None

    def finish(self, job, status, *, result=None, code=None, message=None):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM kb_tasks WHERE id=?',(job['id'],)).fetchone()
            if not row or row['lease_token']!=job['lease_token'] or row['status']!='running':return False
            if status=='succeeded' and not self.valid(db,job['id'],job['lease_token']):status,result,code,message='cancelled',None,'task_stopped','资料权限或会话状态已变化，结果未发布'
            db.execute('UPDATE kb_tasks SET status=?,stage=?,result_json=?,error_code=?,error_message=?,finished_at=?,updated_at=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?',
                (status,'done' if status=='succeeded' else row['stage'],dump(result) if result is not None else None,code,message,now_iso(),now_iso(),job['id']))
        return status=='succeeded'
