"""局部校验事件先落盘再展示；订阅只读，不能领取任务或调用供应商。"""
import json
from app.repository import now_iso

STREAM_SCHEMA = """
CREATE TABLE answer_stream_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 question_job_id TEXT REFERENCES question_jobs(id) ON DELETE CASCADE,
 kb_task_id TEXT REFERENCES kb_tasks(id) ON DELETE CASCADE,
 attempt_no INTEGER NOT NULL CHECK(attempt_no>=1),
 kind TEXT NOT NULL CHECK(kind IN ('reset','block')),
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 CHECK((question_job_id IS NOT NULL AND kb_task_id IS NULL) OR
       (question_job_id IS NULL AND kb_task_id IS NOT NULL))
);
CREATE INDEX ix_answer_stream_question ON answer_stream_events(question_job_id,id);
CREATE INDEX ix_answer_stream_kb ON answer_stream_events(kb_task_id,id);
"""
STREAM_EVENT_LIMIT = 1024


class AnswerStreamStore:
    def __init__(self,repository):self.repository=repository

    @staticmethod
    def column(source):
        if source not in ('question','knowledge'):raise ValueError('invalid_stream_source')
        return 'question_job_id' if source=='question' else 'kb_task_id'

    def remaining_capacity(self,source,identifier):
        column=self.column(source)
        with self.repository.connect() as db:
            count=db.execute('SELECT COUNT(*) FROM answer_stream_events WHERE '+column+'=?',(identifier,)).fetchone()[0]
        return max(0,STREAM_EVENT_LIMIT-count)

    def append(self,source,job,kind,payload):
        column=self.column(source);identifier=job['id'];token=job['lease_token']
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if source=='question':
                from app.question_jobs import QuestionJobStore
                valid=QuestionJobStore._valid(db,identifier,token)
                row=db.execute('SELECT cancel_requested,attempt_count FROM question_jobs WHERE id=?',(identifier,)).fetchone()
                call=db.execute("SELECT 1 FROM question_calls WHERE job_id=? AND role='generation' AND status='intent'",(identifier,)).fetchone()
            else:
                from app.knowledge_tasks import KnowledgeTasks
                valid=KnowledgeTasks.valid(db,identifier,token)
                row=db.execute('SELECT cancel_requested,attempt_count FROM kb_tasks WHERE id=?',(identifier,)).fetchone()
                call=db.execute("SELECT 1 FROM kb_task_calls WHERE task_id=? AND role='generation' AND status='intent'",(identifier,)).fetchone()
            if not valid or not row or row['cancel_requested'] or not call:raise ValueError('stream_execution_stopped')
            count=db.execute('SELECT COUNT(*) FROM answer_stream_events WHERE '+column+'=?',(identifier,)).fetchone()[0]
            if count>=STREAM_EVENT_LIMIT:raise ValueError('stream_event_limit')
            db.execute('INSERT INTO answer_stream_events('+column+',attempt_no,kind,payload_json,created_at) VALUES (?,?,?,?,?)',
                (identifier,row['attempt_count'],kind,json.dumps(payload,ensure_ascii=False,allow_nan=False),now_iso()))

    def read(self,source,identifier,after=0):
        column=self.column(source)
        with self.repository.connect() as db:
            rows=db.execute('SELECT id,attempt_no,kind,payload_json,created_at FROM answer_stream_events WHERE '+column+'=? AND id>? ORDER BY id LIMIT 100',
                            (identifier,after)).fetchall()
        return [dict(id=r['id'],attempt=r['attempt_no'],kind=r['kind'],payload=json.loads(r['payload_json']),created_at=r['created_at']) for r in rows]
