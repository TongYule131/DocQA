"""客户反馈与审核闭环。管理员列表只有运行元数据，正文需客户逐单明确分享。"""
import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
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
    fix_revision:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
    client_reply:str|None=Field(default=None,max_length=1000)


class Evidence(StrictBody):
    case_id:str=Field(max_length=100)
    kind:Literal['original','negative']
    code_identity:str=Field(pattern=r'^[a-f0-9]{64}$')
    fixture_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    report_xml:str=Field(min_length=20,max_length=500000)
    test_name:str=Field(min_length=1,max_length=300)
    run_id:str=Field(min_length=1,max_length=200)


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
            frozen=self.knowledge.tasks.checkpoint(identifier,'auto_document_input') if payload.get('chat_policy') else None
            if frozen:payload={**payload,**frozen['payload']}
            diagnostic=self.knowledge.tasks.checkpoint(identifier,'diagnostic_retrieval_attempt_'+str(row['attempt_count'])) or {}
            return {'question':payload['question'],'answer':json.loads(row['result_json']) if row['result_json'] else None,
                'reproduction':{'task_input_hash':row['input_hash'],'configuration':payload.get('configuration'),
                    'documents':payload.get('documents',[]),'query_plan':payload.get('query_plan'),
                    'input_manifest':diagnostic.get('input_manifest',[]),'budget':payload.get('retrieval_budget'),
                    'chat_policy':payload.get('chat_policy'),'conversation_history_shared':False}}
        self.access.require_question(identifier)
        row = self.questions.get_job(identifier)
        frozen=json.loads(self.questions.store.row(identifier)['snapshot_json'])
        return {'question':row['question'],'answer':self.questions.get_result(row['result_id'])['answer'] if row['result_id'] else None,
            'reproduction':{k:frozen.get(k) for k in ('document_id','parse_version_id','index_id','source_signature',
                'configuration_signature','evidence_signature','task_prompt_version','answer_contract_version')}}

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
            from app.feedback_cases import event
            event(db,identifier,self.owner,'open',{'action':'submitted','share_context':body.share_context})
        return self.get(identifier)

    @staticmethod
    def present(row, *, admin):
        keys = ('id', 'source', 'task_id', 'category', 'share_context', 'status', 'root_cause',
                'revision', 'created_at', 'updated_at', 'regression_case', 'original_retest', 'negative_retest','generation','fix_revision','client_reply')
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
        with self.repository.connect() as db:
            value['history']=[{'status':r['status'],'created_at':r['created_at'],'action':json.loads(r['detail_json']).get('action','review')}
                for r in db.execute('SELECT * FROM feedback_events WHERE feedback_id=? ORDER BY created_at,id',(identifier,))]
        return value

    def list(self, admin=False, limit=100, offset=0, status=None, category=None):
        if admin:
            self.access.admin()
        with self.repository.connect() as db:
            clauses=[] if admin else ['owner_user_id IS ?'];args=[] if admin else [self.owner]
            if status:clauses.append('status=?');args.append(status)
            if category:clauses.append('category=?');args.append(category)
            rows=db.execute('SELECT * FROM answer_feedback'+(' WHERE '+' AND '.join(clauses) if clauses else '')+
                ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(*args,min(100,max(1,limit)),max(0,offset))).fetchall()
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
            from app.runtime import source_identity
            from app.feedback_cases import validate_references
            fix_revision=body.fix_revision or row['fix_revision'] or source_identity()
            if body.status=='open':
                merged={'root_cause':body.root_cause,'regression_case':None,'original_retest':None,'negative_retest':None}
                db.execute('UPDATE answer_feedback SET generation=generation+1,fix_revision=NULL WHERE id=?',(identifier,))
            required = ['root_cause'] if body.status != 'open' else []
            if body.status in ('fixed', 'verified', 'closed'):
                required += ['regression_case']
            if body.status in ('verified', 'closed'):
                required += ['original_retest', 'negative_retest']
            if any(not merged[k] or not str(merged[k]).strip() for k in required):
                raise HTTPException(422, '处理阶段缺少根因、回归样本或原失败／独立负例复验证据')
            if body.status in ('fixed','verified','closed'):validate_references(db,row,merged,fix_revision)
            db.execute('UPDATE answer_feedback SET fix_revision=?,client_reply=COALESCE(?,client_reply) WHERE id=?',
                (fix_revision if body.status in ('fixed','verified','closed') else None,body.client_reply,identifier))
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
    def own_list(limit:int=Query(default=20,ge=1,le=100),offset:int=Query(default=0,ge=0)):
        return service.list(limit=limit,offset=offset)

    @router.get('/api/feedback/{identifier}')
    def own_detail(identifier: str):
        return service.get(identifier)

    @router.get('/api/admin/feedback')
    def admin_list(limit:int=Query(default=20,ge=1,le=100),offset:int=Query(default=0,ge=0),status:str|None=None,category:str|None=None):
        if status is not None and status not in STATES or category is not None and category not in CATEGORIES:raise HTTPException(422,'反馈筛选条件无效')
        return service.list(admin=True,limit=limit,offset=offset,status=status,category=category)

    @router.post('/api/admin/feedback/{identifier}/case')
    def create_regression_case(identifier:str):
        from app.feedback_cases import create_case
        row=create_case(service,identifier)
        return {**{k:v for k,v in row.items() if k!='payload_json'},'payload':json.loads(row['payload_json'])}

    @router.get('/api/admin/feedback/{identifier}/case')
    def regression_case(identifier:str):
        row=service.get(identifier,admin=True)
        if not row['share_context']:raise HTTPException(404,'分享已撤回')
        with access.repository.connect() as db:case=db.execute('SELECT * FROM feedback_cases WHERE feedback_id=? AND generation=?',(identifier,row['generation'])).fetchone()
        if not case or not case['payload_json']:raise HTTPException(404,'尚未建立回归包')
        return {**{k:case[k] for k in case.keys() if k!='payload_json'},'payload':json.loads(case['payload_json'])}

    @router.post('/api/admin/feedback/{identifier}/evidence')
    def register_evidence(identifier:str,body:Evidence):
        from app.feedback_cases import record_evidence
        return record_evidence(service,identifier,body)

    @router.get('/api/admin/quality')
    def quality():
        access.admin()
        with access.repository.connect() as db:
            business="""kind='question' AND (COALESCE(json_extract(input_json,'$.route.kind'),'KB') NOT IN ('SYSTEM','AUTO')
                OR json_extract(input_json,'$.route.kind')='AUTO' AND EXISTS(SELECT 1 FROM kb_task_checkpoints k
                WHERE k.task_id=kb_tasks.id AND k.name='auto_document_input'
                AND json_extract(k.payload_json,'$.payload.route.kind') IN ('KB','MCP')))"""
            statuses={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM kb_tasks WHERE '+business+' GROUP BY status')}
            answered=db.execute("SELECT COUNT(*) FROM kb_tasks WHERE "+business+" AND status='succeeded' AND json_extract(result_json,'$.status')='answered'").fetchone()[0]
            general=db.execute("SELECT COUNT(*) FROM kb_tasks WHERE status='succeeded' AND json_extract(result_json,'$.response_kind')='general'").fetchone()[0]
            feedback={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM answer_feedback GROUP BY status')}
            causes={r[0]:r[1] for r in db.execute('SELECT root_cause,COUNT(*) FROM answer_feedback WHERE root_cause IS NOT NULL GROUP BY root_cause')}
            evidence=db.execute('SELECT COUNT(*) FROM feedback_evidence').fetchone()[0]
        return {'business_tasks':{'total':sum(statuses.values()),'statuses':statuses,'answered':answered},
            'general_answers':general,'feedback':feedback,'root_causes':causes,'registered_engineering_evidence':evidence,
            'human_semantic_review':{'reviewed':None,'passed':None,'note':'未登记独立人工rubric，不能由工程通过或工单关闭推断语义通过率'},
            'citation_engineering':{'note':'结果发布继续执行来源和引用硬校验；不以工单关闭数代替引用通过率'},
            'provider_calls':0,'content_policy':'只含汇总计数，无客户问题或正文'}

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
            db.execute('BEGIN IMMEDIATE')
            from app.feedback_cases import withdraw
            withdraw(db,identifier,service.owner)
        return service.get(identifier)

    app.include_router(router)
