"""客户反馈与审核闭环。管理员列表只有运行元数据，正文需客户逐单明确分享。"""
import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field
from typing import Literal

from app.product_routes import StrictBody
from app.repository import now_iso

CATEGORIES = ('number_unit', 'citation_page', 'missing', 'off_topic', 'source_conflict', 'expression', 'helpful')
STATES = ('open', 'reproduced', 'fixed', 'verified', 'closed')
CAUSES = ('parsing', 'retrieval', 'ranking', 'packing', 'planning', 'generation', 'validation', 'presentation', 'other')


class Submit(StrictBody):
    source: Literal['knowledge', 'question']
    task_id: str = Field(min_length=1, max_length=150)
    request_key: str = Field(min_length=1, max_length=100)
    category: Literal['number_unit', 'citation_page', 'missing', 'off_topic', 'source_conflict', 'expression', 'helpful']
    comment: str = Field(default='', max_length=2000)
    share_context: bool = False


class Review(StrictBody):
    expected_revision: int = Field(ge=1)
    status: Literal['open', 'reproduced', 'fixed', 'verified', 'closed']
    root_cause: Literal['parsing', 'retrieval', 'ranking', 'packing', 'planning', 'generation', 'validation', 'presentation', 'other'] | None = None
    regression_case: str | None = Field(default=None, max_length=300)
    original_retest: str | None = Field(default=None, max_length=300)
    negative_retest: str | None = Field(default=None, max_length=300)


class Feedback:
    def __init__(self, access, knowledge, questions):
        self.access, self.knowledge, self.questions = access, knowledge, questions
        self.repository = access.repository

    @property
    def owner(self):
        return self.access.actor['id'] if self.access.actor else None

    def task(self, source, identifier):
        if source == 'knowledge':
            row = self.knowledge.require_task(identifier)
            if row['kind'] != 'question':
                raise HTTPException(409, '仅支持回答反馈')
            payload = json.loads(row['input_json'])
            return {'question': payload['question'], 'answer': json.loads(row['result_json']) if row['result_json'] else None}
        self.access.require_question(identifier)
        row = self.questions.get_job(identifier)
        return {'question': row['question'], 'answer': self.questions.get_result(row['result_id'])['answer'] if row['result_id'] else None}

    def submit(self, body):
        context = self.task(body.source, body.task_id)
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True) if body.share_context else None
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM answer_feedback WHERE owner_user_id IS ? AND request_key=?', (self.owner, body.request_key)).fetchone()
            if old:
                if any(old[k] != getattr(body, k) for k in ('source', 'task_id', 'category', 'comment', 'share_context')):
                    raise HTTPException(409, '请求键已用于不同反馈')
                return self.present(dict(old), admin=False)
            identifier, stamp = 'feedback-' + uuid4().hex, now_iso()
            db.execute('INSERT INTO answer_feedback(id,owner_user_id,source,task_id,request_key,category,comment,share_context,context_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier, self.owner, body.source, body.task_id, body.request_key, body.category, body.comment, int(body.share_context), encoded, stamp, stamp))
        return self.get(identifier)

    @staticmethod
    def present(row, *, admin):
        keys = ('id', 'source', 'task_id', 'category', 'share_context', 'status', 'root_cause',
                'revision', 'created_at', 'updated_at', 'regression_case', 'original_retest', 'negative_retest')
        value = {k: row[k] for k in keys}
        if not admin:
            value['comment'] = row['comment']
        return value

    def get(self, identifier, admin=False):
        if admin:
            self.access.admin()
        with self.repository.connect() as db:
            row = db.execute('SELECT * FROM answer_feedback WHERE id=?', (identifier,)).fetchone()
        if not row or not admin and row['owner_user_id'] != self.owner:
            raise HTTPException(404, '反馈不存在或未获授权')
        value = self.present(dict(row), admin=admin)
        if admin and row['share_context']:
            # 客户主动分享的是提交时的回答快照，不授予其他会话或业务原件权限。
            value['shared_context'] = json.loads(row['context_json']) if row['context_json'] else None
            value['comment'] = row['comment']
        return value

    def list(self, admin=False, limit=100):
        if admin:
            self.access.admin()
        with self.repository.connect() as db:
            rows = db.execute('SELECT * FROM answer_feedback ' + ('' if admin else 'WHERE owner_user_id IS ? ') +
                'ORDER BY created_at DESC LIMIT ?', (limit,) if admin else (self.owner, limit)).fetchall()
        result = []
        for row in rows:
            result.append(self.present(dict(row), admin=admin))
        return result

    def review(self, identifier, body):
        self.access.admin()
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM answer_feedback WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise HTTPException(404, '反馈不存在')
            if row['revision'] != body.expected_revision:
                raise HTTPException(409, '反馈已更新，请刷新')
            if body.status != 'open' and STATES.index(body.status) != STATES.index(row['status']) + 1:
                raise HTTPException(409, '请按复现、修复、验证、关闭顺序处理')
            merged = {k: getattr(body, k) or row[k] for k in ('root_cause', 'regression_case', 'original_retest', 'negative_retest')}
            required = ['root_cause'] if body.status != 'open' else []
            if body.status in ('fixed', 'verified', 'closed'):
                required += ['regression_case']
            if body.status in ('verified', 'closed'):
                required += ['original_retest', 'negative_retest']
            if any(not merged[k] or not str(merged[k]).strip() for k in required):
                raise HTTPException(422, '处理阶段缺少根因、回归样本或原失败／独立负例复验证据')
            db.execute('UPDATE answer_feedback SET status=?,root_cause=?,regression_case=?,original_retest=?,negative_retest=?,revision=revision+1,updated_at=? WHERE id=?',
                       (body.status, *merged.values(), now_iso(), identifier))
            db.execute('INSERT INTO feedback_events VALUES (?,?,?,?,?,?)', ('fevent-' + uuid4().hex, identifier,
                self.owner, body.status, json.dumps(merged, ensure_ascii=False), now_iso()))
        return self.get(identifier, admin=True)


def install_feedback_routes(app, access, questions):
    service = Feedback(access, app.state.knowledge_questions, questions)
    app.state.feedback = service
    router = APIRouter()

    @router.post('/api/feedback', status_code=201)
    def submit(body: Submit):
        return service.submit(body)

    @router.get('/api/feedback')
    def own_list():
        return service.list()

    @router.get('/api/feedback/{identifier}')
    def own_detail(identifier: str):
        return service.get(identifier)

    @router.get('/api/admin/feedback')
    def admin_list():
        return service.list(admin=True)

    @router.get('/api/admin/feedback/{identifier}')
    def admin_detail(identifier: str):
        return service.get(identifier, admin=True)

    @router.patch('/api/admin/feedback/{identifier}')
    def review(identifier: str, body: Review):
        return service.review(identifier, body)

    @router.delete('/api/feedback/{identifier}/shared-context')
    def revoke(identifier: str):
        service.get(identifier)
        with access.repository.connect() as db:
            db.execute('UPDATE answer_feedback SET share_context=0,context_json=NULL,revision=revision+1,updated_at=? WHERE id=? AND owner_user_id IS ?', (now_iso(), identifier, service.owner))
        return service.get(identifier)

    app.include_router(router)
