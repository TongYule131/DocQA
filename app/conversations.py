"""持久化单资料会话。创建/恢复只读本地库，收费任务沿用QuestionJobService。"""
from uuid import uuid4

from fastapi import HTTPException

from app.conversation_memory import MEMORY_VERSION
from app.repository import now_iso


ACTIVE = ('queued', 'running')


def stop_conversation_jobs(db, identifier):
    stamp = now_iso()
    ids = [r[0] for r in db.execute("SELECT id FROM question_jobs WHERE conversation_id=? AND status IN ('queued','running')", (identifier,))]
    for job in ids:
        db.execute("UPDATE question_calls SET status='uncertain',error_code='conversation_closed',settled_at=? WHERE job_id=? AND status='intent'", (stamp, job))
        db.execute("UPDATE question_jobs SET status='cancelled',cancel_requested=1,lease_token=NULL,lease_expires_at=NULL,"
                   "finished_at=?,updated_at=?,error_code='conversation_closed',error_message='会话已关闭；在途请求可能继续计费' WHERE id=?", (stamp, stamp, job))
        db.execute("UPDATE question_attempts SET status='cancelled',finished_at=?,error_code='conversation_closed' WHERE job_id=? AND status='running'", (stamp, job))


class Conversations:
    def __init__(self, access, questions):
        self.access, self.repository, self.questions = access, access.repository, questions

    @property
    def owner(self):
        return self.access.actor['id'] if self.access.team_mode else None

    def require(self, identifier, *, writable=False, db=None):
        if db is None:
            with self.repository.connect() as connection:
                return self.require(identifier, writable=writable, db=connection)
        row = db.execute('SELECT * FROM conversations WHERE id=? AND owner_user_id IS ? AND deleted_at IS NULL',
                         (identifier, self.owner)).fetchone()
        if not row:
            raise HTTPException(404, '会话不存在或未获授权')
        self.access.require_document(row['document_id'])
        if writable and row['archived']:
            raise HTTPException(409, '会话已归档，请恢复或新建会话')
        return dict(row)

    def list(self, archived=False):
        with self.repository.connect() as db:
            rows = db.execute('SELECT * FROM conversations WHERE owner_user_id IS ? AND archived=? AND deleted_at IS NULL ORDER BY updated_at DESC,rowid DESC',
                              (self.owner, int(archived))).fetchall()
        result = []
        for row in rows:
            try:
                self.access.require_document(row['document_id'])
            except HTTPException:
                continue
            result.append(dict(row))
        return result

    def create(self, document_id, key):
        self.access.require_document(document_id)
        if self.access.attachment(document_id):
            raise HTTPException(409, '临时资料仅用于上传时建立的会话，请新上传资料建立另一会话')
        document = self.repository.get(document_id)
        if not document:
            raise HTTPException(404, '资料不存在或未获授权')
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM conversations WHERE owner_user_id IS ? AND request_key=?', (self.owner, key)).fetchone()
            if prior:
                if prior['document_id'] != document_id or prior['deleted_at']:
                    raise HTTPException(409, '此请求键已用于其他会话')
                return dict(prior)
            identifier, stamp = 'conv-' + uuid4().hex, now_iso()
            db.execute('INSERT INTO conversations(id,owner_user_id,document_id,title,request_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                       (identifier, self.owner, document_id, '新会话', key, stamp, stamp))
        return self.require(identifier)

    def memory(self, conversation, db=None):
        if db is None:
            with self.repository.connect() as connection:
                return self.memory(conversation, connection)
        questions = []
        if conversation['memory_enabled']:
            questions = [r[0][:600] for r in db.execute("SELECT t.question FROM conversation_turns t JOIN question_jobs j ON j.id=t.job_id JOIN question_results r ON r.id=j.result_id "
                "WHERE t.conversation_id=? AND t.turn_no>? AND j.status='succeeded' AND COALESCE(json_extract(r.answer_json,'$.response_kind'),'knowledge')!='system' ORDER BY t.turn_no DESC LIMIT 3",
                (conversation['id'], conversation['memory_after_turn']))][::-1]
        return {'enabled': bool(conversation['memory_enabled']), 'epoch': conversation['memory_epoch'],
                'context': {'version': MEMORY_VERSION, 'notes': conversation['memory_notes'] if conversation['memory_enabled'] else '',
                            'recent_questions': questions}}

    def detail(self, identifier):
        conversation = self.require(identifier)
        with self.repository.connect() as db:
            ids = db.execute('SELECT turn_no,job_id,request_key FROM conversation_turns WHERE conversation_id=? ORDER BY turn_no', (identifier,)).fetchall()
        turns = []
        for number, job_id, key in ids:
            self.access.require_question(job_id)
            job = self.questions.get_job(job_id)
            turns.append({'turn_no': number, 'request_key': key, 'job': job, 'result': self.questions.get_result(job['result_id']) if job['result_id'] else None})
        return {**conversation, 'memory': self.memory(conversation), 'turns': turns,
                'attachment': self.access.attachment(conversation['document_id']),
                'document': self.access.present_document(self.repository.get(conversation['document_id'])).model_dump()}

    def update(self, identifier, body):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            conversation = self.require(identifier, db=db)
            if body.expected_revision != conversation['revision']:
                raise HTTPException(409, '会话已变更，请刷新后操作')
            active = db.execute("SELECT 1 FROM question_jobs WHERE conversation_id=? AND status IN ('queued','running')", (identifier,)).fetchone()
            memory_change = body.clear_memory or body.memory_enabled is not None or body.memory_notes is not None
            if active and memory_change:
                raise HTTPException(409, '请等待本轮完成或停止后再修改记忆')
            if body.archived is True or body.deleted is True:
                stop_conversation_jobs(db, identifier)
            latest = db.execute('SELECT COALESCE(MAX(turn_no),0) FROM conversation_turns WHERE conversation_id=?', (identifier,)).fetchone()[0]
            toggled = body.memory_enabled is not None and int(body.memory_enabled) != conversation['memory_enabled']
            db.execute('UPDATE conversations SET title=?,memory_enabled=?,memory_notes=?,memory_epoch=?,memory_after_turn=?,archived=?,deleted_at=?,revision=revision+1,updated_at=? WHERE id=?',
                ((body.title or conversation['title']).strip(), int(body.memory_enabled) if body.memory_enabled is not None else conversation['memory_enabled'],
                 '' if body.clear_memory else body.memory_notes if body.memory_notes is not None else conversation['memory_notes'],
                 conversation['memory_epoch'] + int(memory_change), latest if body.clear_memory or toggled else conversation['memory_after_turn'],
                 int(body.archived) if body.archived is not None else conversation['archived'], now_iso() if body.deleted else None, now_iso(), identifier))
        return {'deleted': True} if body.deleted else self.detail(identifier)

    def submit(self, identifier, payload, request_id):
        conversation = self.require(identifier, writable=True)
        with self.repository.connect() as db:
            prior = db.execute('SELECT * FROM conversation_turns WHERE conversation_id=? AND request_key=?', (identifier, payload.idempotency_key)).fetchone()
            if prior:
                if prior['question'] != payload.question.strip():
                    raise HTTPException(409, '此请求键已用于其他问题')
                job = self.questions.get_job(prior['job_id'])
                return {'job': job, 'reused': True}, 202 if job['status'] in ACTIVE else 200
            memory = self.memory(conversation, db)
        published = self.access.require_document(conversation['document_id'])
        context = {'id': identifier, 'revision': conversation['revision'], 'memory_epoch': conversation['memory_epoch'],
                   'request_key': payload.idempotency_key, 'memory': memory['context'] if memory['enabled'] else None}
        from app.intent_management import IntentManagement
        with self.repository.connect() as db:
            base_row=db.execute('SELECT knowledge_base_id FROM knowledge_base_snapshots WHERE id=?',(published['snapshot_id'],)).fetchone() if published else None
        intent_base_id=base_row[0] if base_row else None
        resolution=IntentManagement(self.repository).evaluate(payload.question.strip(),intent_base_id,scope='document')
        return self.questions.submit(conversation['document_id'],
            {'question': payload.question.strip(), 'idempotency_key': 'conv/' + identifier + '/' + payload.idempotency_key},
            request_id=request_id, owner_user_id=self.owner,
            knowledge_snapshot_id=published['snapshot_id'] if published else None, conversation=context,
            intent_resolution=resolution, intent_base_id=intent_base_id)
