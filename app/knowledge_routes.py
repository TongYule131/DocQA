"""知识库任务、来源维护、意图管理与链路追踪接口。"""
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter,HTTPException,Query,UploadFile,File,Header,Request
from fastapi.responses import FileResponse,PlainTextResponse,RedirectResponse
from pydantic import Field

from app.product_routes import StrictBody
from app.knowledge_pipeline import KnowledgePipeline
from app.knowledge_questions import KnowledgeQuestions
from app.intent_management import IntentManagement
from app.knowledge_traces import KnowledgeTraces
from app.repository import now_iso


class IndexSubmit(StrictBody):
    request_key:str=Field(min_length=1,max_length=100)
    rebuild:bool=False


class ChunkEdit(StrictBody):
    chunk_ids:list[str]=Field(min_length=1,max_length=20)
    start:int=Field(ge=0)
    end:int=Field(gt=0)


class ChunkRevision(StrictBody):
    version_id:str=Field(min_length=1,max_length=150)
    edits:list[ChunkEdit]=Field(min_length=1,max_length=2000)
    reason:str=Field(min_length=1,max_length=500,pattern=r'\S')


class IntentBody(StrictBody):
    code:str=Field(min_length=2,max_length=64)
    name:str=Field(min_length=1,max_length=80,pattern=r'\S')
    parent_id:str|None=None
    kind:Literal['KB','SYSTEM','MCP']
    base_id:str|None=None
    tool_id:str|None=Field(default=None,max_length=100)
    description:str=Field(default='',max_length=1000)
    examples:list[str]=Field(default_factory=list,max_length=50)
    response_text:str=Field(default='',max_length=1000)
    top_k:int|None=Field(default=None,ge=1,le=32)
    sort_order:int=Field(default=0,ge=0,le=10000)
    enabled:bool=True
    match_mode:Literal['exact','interaction','keywords']='exact'
    interaction_kind:Literal['welcome','about','thanks','correction']|None=None
    keywords:list[str]=Field(default_factory=list,max_length=20)
    expected_revision:int|None=Field(default=None,ge=1)


class IntentTestBody(StrictBody):
    questions:list[str]=Field(min_length=1,max_length=100)
    scope:Literal['document','knowledge']='document'
    context:dict|None=None
    base_id:str|None=Field(default=None,max_length=100)


class SemanticPrepareBody(StrictBody):
    max_requests:int=Field(default=4,ge=1,le=20)
    request_key:str=Field(min_length=1,max_length=100)
    acknowledge_uncertain:bool=False


class SemanticCalibrationBody(StrictBody):
    cases:list[dict]=Field(min_length=6,max_length=200)
    threshold:float=Field(ge=0,le=1)
    margin:float=Field(ge=0,le=1)


class ConversationBody(StrictBody):
    base_id:str=Field(min_length=1,max_length=100)
    request_key:str=Field(min_length=1,max_length=100)


class QuestionBody(StrictBody):
    question:str=Field(min_length=1,max_length=4000,pattern=r'\S')
    request_key:str=Field(min_length=1,max_length=100)


class RetryBody(StrictBody):
    acknowledge_uncertain:bool=False


class MemoryBody(StrictBody):
    enabled:bool
    clear:bool=False


class ToolBody(StrictBody):
    id:str=Field(min_length=2,max_length=64,pattern=r'^[a-z][a-z0-9-]+$')
    name:str=Field(min_length=1,max_length=80)
    endpoint:str=Field(min_length=8,max_length=1000)
    tool_name:str=Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_.-]+$')
    auth_env:str|None=None
    input_schema:dict
    protocol_version:Literal['2025-11-25']='2025-11-25'
    read_only:bool=True
    enabled:bool=False
    expected_revision:int|None=Field(default=None,ge=1)


def install_knowledge_routes(app,access,rag):
    pipeline=KnowledgePipeline(access);questions=KnowledgeQuestions(access,rag)
    intents=IntentManagement(access.repository);traces=KnowledgeTraces(access.repository);router=APIRouter()
    app.state.knowledge_questions=questions
    def admin():access.admin()

    @router.post('/api/admin/knowledge/bases/{base_id}/documents',status_code=201)
    def upload(base_id:str,file:UploadFile=File(...),idempotency_key:str=Header(alias='Idempotency-Key',min_length=1,max_length=200,pattern=r'\S')):
        admin();access.base(base_id)
        from app.uploads import store_upload
        def bind(db,document):
            if not db.execute('SELECT 1 FROM knowledge_bases WHERE id=?',(base_id,)).fetchone():raise HTTPException(409,'知识库已变更，请刷新')
            linked=db.execute('SELECT knowledge_base_id FROM knowledge_base_documents WHERE document_id=?',(document.id,)).fetchone()
            if linked and linked[0]!=base_id:raise HTTPException(409,'原上传已绑定其他知识库')
            db.execute('INSERT OR IGNORE INTO knowledge_base_documents(knowledge_base_id,document_id,added_at) VALUES (?,?,?)',(base_id,document.id,now_iso()))
            db.execute('UPDATE knowledge_bases SET updated_at=? WHERE id=?',(now_iso(),base_id))
        return store_upload(access.repository,access.settings,file,idempotency_key,scope='knowledge/'+base_id,link=bind)

    @router.post('/api/admin/knowledge/documents/{identifier}/index-tasks',status_code=202)
    def submit_index(identifier:str,body:IndexSubmit):return questions.present(pipeline.submit_index(identifier,body.request_key,rebuild=body.rebuild))

    @router.get('/api/admin/knowledge/documents/{identifier}/chunks')
    def chunks(identifier:str,version_id:str|None=None):return pipeline.chunk_detail(identifier,version_id)

    @router.post('/api/admin/knowledge/documents/{identifier}/chunk-revisions',status_code=201)
    def revise(identifier:str,body:ChunkRevision):return pipeline.revise(identifier,body.version_id,[e.model_dump() for e in body.edits],body.reason)

    @router.get('/api/admin/knowledge/index-tasks')
    def index_tasks(document_id:str|None=None):
        admin()
        with access.repository.connect() as db:
            rows=db.execute("SELECT * FROM kb_tasks WHERE kind='index'"+(' AND document_id=?' if document_id else '')+' ORDER BY created_at DESC LIMIT 100',(document_id,) if document_id else ()).fetchall()
        return [questions.present(dict(r)) for r in rows]

    @router.get('/api/kb-tasks/{identifier}')
    def task(identifier:str):return questions.present(questions.require_task(identifier))

    @router.post('/api/kb-tasks/{identifier}/cancel')
    def cancel(identifier:str):
        row=questions.require_task(identifier)
        with access.repository.connect() as db:
            db.execute("UPDATE kb_tasks SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated_at=? WHERE id=? AND status IN ('queued','running')",(now_iso(),identifier))
        return questions.present(pipeline.tasks.row(identifier))

    @router.post('/api/kb-tasks/{identifier}/retry')
    def retry(identifier:str,body:RetryBody):
        questions.require_task(identifier)
        with access.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM kb_tasks WHERE id=?',(identifier,)).fetchone()
            if row['status'] not in ('failed','needs_attention'):raise HTTPException(409,'只有失败或待确认任务可以重试')
            complete=db.execute("SELECT 1 FROM kb_task_checkpoints WHERE task_id=? AND name='validated_protocol'",(identifier,)).fetchone()
            if row['requests_used']>=row['max_requests'] and not complete:raise HTTPException(409,'原任务请求预算已用完，重试不重置预算')
            if db.execute("SELECT 1 FROM kb_task_calls WHERE task_id=? AND status IN ('intent','uncertain') AND COALESCE(error_code,'')!='retry_acknowledged'",(identifier,)).fetchone():
                if not body.acknowledge_uncertain:raise HTTPException(409,'存在可能已计费的未确认调用，须明确确认风险后重试')
                db.execute("UPDATE kb_task_calls SET status='uncertain',error_code='retry_acknowledged' WHERE task_id=? AND status IN ('intent','uncertain')",(identifier,))
            payload=json.loads(row['input_json'])
            if payload.get('conversation_id'):
                conv=db.execute('SELECT memory_epoch FROM kb_conversations WHERE id=?',(payload['conversation_id'],)).fetchone()
                if not conv or conv[0]!=payload['memory_epoch']:raise HTTPException(409,'记忆版本已变化，请提交新问题')
            if payload.get('conversation_id') and db.execute("SELECT 1 FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status IN ('queued','running')",(payload['conversation_id'],)).fetchone():raise HTTPException(409,'会话已有活动任务')
            from app.resource_governance import admit
            admit(db,access.repository,row['owner_user_id'])
            db.execute("UPDATE kb_tasks SET status='queued',cancel_requested=0,lease_token=NULL,lease_expires_at=NULL,error_code=NULL,error_message=NULL,updated_at=?,finished_at=NULL WHERE id=?",(now_iso(),identifier))
        return questions.present(pipeline.tasks.row(identifier))

    @router.get('/api/kb-tasks/{identifier}/export')
    def export(identifier:str):
        row=questions.require_task(identifier)
        if row['status']!='succeeded':raise HTTPException(409,'任务尚无已发布结果')
        return PlainTextResponse(row['result_json'],media_type='application/json',headers={'Content-Disposition':f'attachment; filename="{identifier}.json"'})

    @router.get('/api/kb-conversations')
    def conversations():return questions.list()

    @router.post('/api/kb-conversations',status_code=201)
    def create(body:ConversationBody):return questions.create(body.base_id,body.request_key)

    @router.get('/api/kb-conversations/{identifier}')
    def conversation(identifier:str):return questions.detail(identifier)

    @router.post('/api/kb-conversations/{identifier}/turns',status_code=202)
    def submit(identifier:str,body:QuestionBody):return questions.submit(identifier,body.question.strip(),body.request_key)

    @router.patch('/api/kb-conversations/{identifier}/memory')
    def memory(identifier:str,body:MemoryBody):return questions.memory(identifier,body.enabled,body.clear)

    @router.get('/api/admin/intents')
    def list_intents():admin();return intents.list()

    @router.get('/api/admin/intents/semantic/status')
    def semantic_status():
        admin()
        from app.semantic_intents import SemanticIntents
        policy=SemanticIntents(access.repository).policy(intents.list(),access.index.embedding.signature)
        return {'enabled':access.settings.semantic_intent_enabled,'calibrated':bool(policy),
                'threshold':policy['threshold'] if policy else None,'margin':policy['margin'] if policy else None,'provider_calls':0}

    @router.post('/api/admin/intents/semantic/prepare')
    def prepare_semantic(body:SemanticPrepareBody):
        admin()
        from app.semantic_intents import SemanticIntents
        from app.knowledge_tasks import configuration,KnowledgeTasks
        planned=SemanticIntents(access.repository).plan(intents.list(),access.index.embedding,max_requests=body.max_requests)
        with access.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            unresolved=db.execute("SELECT c.id FROM kb_task_calls c JOIN kb_tasks t ON t.id=c.task_id WHERE json_extract(t.input_json,'$.operation')='intent_examples' AND json_extract(t.input_json,'$.semantic_configuration')=? AND c.status IN ('intent','uncertain') AND COALESCE(c.error_code,'')!='retry_acknowledged'",(planned['semantic_configuration'],)).fetchall()
            if unresolved and not body.acknowledge_uncertain:raise HTTPException(409,'存在未确认的示例准备调用，核实可能重复计费后才能明确重试')
            for call in unresolved:db.execute("UPDATE kb_task_calls SET error_code='retry_acknowledged' WHERE id=?",(call['id'],))
        payload={'operation':'intent_examples','configuration':configuration(access.settings,access.index.embedding),
            'semantic_work':planned['work'],'semantic_configuration':planned['semantic_configuration']}
        task=KnowledgeTasks(access.repository).create('index',payload,'semantic/'+body.request_key,
            owner=access.actor['id'] if access.actor else None,max_requests=max(1,planned['expected_requests']))
        return {'task_id':task['id'],'status':task['status'],'expected_http_requests':planned['expected_requests'],'provider_calls':0}

    @router.post('/api/admin/intents/semantic/calibrate')
    def calibrate_semantic(body:SemanticCalibrationBody):
        admin()
        from app.semantic_intents import SemanticIntents
        return SemanticIntents(access.repository).calibrate(intents.list(),access.index.embedding.signature,body.cases,threshold=body.threshold,margin=body.margin)

    @router.post('/api/admin/intents/test')
    def test_intents(body:IntentTestBody):
        admin()
        if any(not q.strip() or len(q)>500 for q in body.questions):raise HTTPException(422,'每个测试问题须为1～500字符')
        from app.conversation_memory import valid_entity_context
        if not valid_entity_context(body.context):raise HTTPException(422,'用户上下文格式或范围无效')
        if body.base_id:access.base(body.base_id)
        rows=intents.list()
        from app.intent_routing import decide
        from app.query_planning import plan_query, describe_plan, execution_budget
        from app.knowledge_tools import ToolRegistry
        tools={t['id']:t for t in ToolRegistry(access.repository).list()}
        results=[]
        for question in body.questions:
            resolution=intents.evaluate(question,body.base_id,body.scope,context=body.context,provider_signature=access.index.embedding.signature)
            route,decision,plan=resolution['route'],resolution['decision'],resolution['query_plan']
            availability={'ready':True,'reason':'执行时仍核对授权、固定快照与来源；测试不执行问答'}
            if resolution['execution']['action'] in ('source_unavailable','split_request'):
                availability={'ready':False,'reason':decision['reason']}
            if route['kind']=='MCP':
                tool=tools.get(route.get('tool_id'))
                if not tool or not tool['enabled'] or not tool['read_only'] or not tool['auth_configured']:
                    availability={'ready':False,'reason':'工具未登记、停用、非只读或缺少服务端凭据，不能执行'}
            elif route['kind']=='KB' and body.scope=='knowledge':
                if not body.base_id:availability={'ready':False,'reason':'请先选择获授权的知识库'}
                elif not access.base(body.base_id)['active_snapshot_id']:
                    availability={'ready':False,'reason':'当前知识库没有可用发布快照，尚不能检索'}
            results.append({'question':question,'decision':decision,'query_plan':describe_plan(plan),'understanding':resolution['understanding'],'semantic_pending':resolution.get('semantic_pending',False),
                'execution':resolution['execution'],
                'retrieval_budget':execution_budget(plan,access.settings,route),'availability':availability,
                'response_preview':route.get('response_text') if route['kind']=='SYSTEM' else None,'top_k':route.get('top_k')})
        return {'results':results,'supplier_calls':0,'persisted':False,'note':'与会话使用同一规则；测试不创建问答任务，不执行检索、模型或工具'}

    @router.get('/api/admin/mcp-tools')
    def tools():
        admin();from app.knowledge_tools import ToolRegistry
        return ToolRegistry(access.repository).list()

    @router.post('/api/admin/mcp-tools',status_code=201)
    def register_tool(body:ToolBody):
        admin();from app.knowledge_tools import ToolRegistry
        return ToolRegistry(access.repository).save(body.model_dump())

    @router.post('/api/admin/intents',status_code=201)
    def create_intent(body:IntentBody):admin();return intents.save(body.model_dump())

    @router.put('/api/admin/intents/{identifier}')
    def edit_intent(identifier:str,body:IntentBody):admin();return intents.save(body.model_dump(),identifier)

    @router.delete('/api/admin/intents/{identifier}')
    def remove_intent(identifier:str,revision:int=Query(ge=1)):
        admin();intents.delete(identifier,revision);return {'status':'deleted'}

    @router.get('/api/admin/traces')
    def list_traces(q:str=Query(default='',max_length=100),status:str='',page:int=Query(default=1,ge=1)):
        admin();return traces.list(q,status,page)

    @router.get('/api/admin/traces/{identifier}')
    def trace(identifier:str):
        admin();value=traces.detail(identifier)
        if value is None:raise HTTPException(404,'链路记录不存在')
        return value

    def page_handler(path):
        def page(request:Request):
            if path.startswith('/admin/'):admin()
            if path=='/app/knowledge':
                from urllib.parse import urlencode
                base=request.query_params.get('base')
                return RedirectResponse('/app'+('?' + urlencode({'base':base}) if base else ''),status_code=303)
            return FileResponse(Path(__file__).parent/'web'/('knowledge-chat.html' if path=='/app/knowledge' else 'knowledge-admin.html'))
        return page
    for path in ['/admin/intents','/admin/traces','/admin/chunks','/app/knowledge']:
        router.add_api_route(path,page_handler(path),methods=['GET'],include_in_schema=False)
    app.include_router(router)
