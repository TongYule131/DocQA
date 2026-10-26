"""统一会话与自动入库接口；查询、轮询和恢复不触发外部调用。"""
from fastapi import APIRouter,File,Form,Header,HTTPException,UploadFile
from pydantic import Field
from typing import Literal
from app.product_routes import StrictBody
from app.conversation_routes import ConversationUpdate
from app.unified_chat import UnifiedChat
from app.ingestion import Ingestion
from app.repository import now_iso


class Key(StrictBody):
    request_key:str=Field(min_length=1,max_length=100,pattern=r'\S')


class ChatCreate(Key):
    base_id:str=Field(min_length=1,max_length=150)
    document_ids:list[str]=Field(default_factory=list,max_length=50)


class Turn(Key):
    question:str=Field(min_length=1,max_length=4000,pattern=r'\S')
    document_ids:list[str]|None=Field(default=None,max_length=50)


class Retry(StrictBody):
    acknowledge_uncertain:bool=False


class Entities(StrictBody):
    expected_revision:int=Field(ge=0)
    names:list[str]=Field(max_length=12)
    mode:Literal['auto','replace','merge']='replace'


class Clarify(StrictBody):
    names:list[str]=Field(min_length=1,max_length=30)
    request_key:str=Field(min_length=1,max_length=100)


class Policy(StrictBody):
    activation_policy:Literal['auto','review']
    expected_revision:int=Field(ge=0)


def install_unified_routes(app,access,rag,legacy):
    knowledge=app.state.knowledge_questions;chat=UnifiedChat(access,knowledge,legacy);ingestion=Ingestion(access)
    app.state.unified_chat=chat;router=APIRouter()

    @router.get('/api/chat/scopes')
    def scopes():return chat.scopes()

    @router.post('/api/chat/temporary',status_code=201)
    def temporary(body:Key):return chat.temporary(body.request_key)

    @router.delete('/api/chat/temporary/{identifier}')
    def revoke_temporary(identifier:str):
        base=access.base(identifier)
        if base['kind']!='temporary':raise HTTPException(409,'仅支持在此移除自己的临时库')
        with access.repository.connect() as db:db.execute('UPDATE knowledge_bases SET revoked_at=? WHERE id=?',(now_iso(),identifier))
        return {'status':'revoked'}

    @router.get('/api/chat/conversations')
    def conversations(archived:bool=False):return chat.list(archived)

    @router.post('/api/chat/conversations',status_code=201)
    def create(body:ChatCreate):return chat.create(body.base_id,body.request_key,body.document_ids)

    @router.get('/api/chat/conversations/{identifier}')
    def detail(identifier:str):return chat.detail(identifier)

    @router.patch('/api/chat/conversations/{identifier}')
    def update(identifier:str,body:ConversationUpdate):return chat.update(identifier,body)

    @router.patch('/api/chat/conversations/{identifier}/entities')
    def edit_entities(identifier:str,body:Entities):
        from app.entity_memory import edit
        edit(knowledge,chat.bridge(identifier),body.names,body.expected_revision,body.mode)
        return chat.detail(identifier)

    @router.post('/api/chat/conversations/{identifier}/turns',status_code=202)
    def turn(identifier:str,body:Turn):return chat.submit(identifier,body.question.strip(),body.request_key,body.document_ids)

    @router.post('/api/chat/conversations/{identifier}/clarifications/{clarification}/resolve')
    def resolve_clarification(identifier:str,clarification:str,body:Clarify):
        from app.clarifications import resolve
        return resolve(chat,identifier,clarification,body.names,body.request_key)

    @router.post('/api/chat/conversations/{identifier}/clarifications/{clarification}/cancel')
    def cancel_clarification(identifier:str,clarification:str):
        from app.clarifications import cancel
        return cancel(chat,identifier,clarification)

    @router.post('/api/chat/conversations/{identifier}/submissions/{submission}/cancel')
    def cancel_waiting(identifier:str,submission:str):
        conv=chat.detail(identifier)
        with access.repository.connect() as db:
            db.execute("UPDATE chat_submissions SET status='cancelled',error_message='已取消等待',updated_at=? WHERE id=? AND conversation_id=? AND status='waiting'",(now_iso(),submission,conv['canonical_id']))
        return chat.detail(identifier)

    @router.get('/api/ingestions')
    def ingestions(base_id:str):
        access.base(base_id)
        with access.repository.connect() as db:ids=[r[0] for r in db.execute('SELECT id FROM ingestion_jobs WHERE base_id=? ORDER BY created_at DESC LIMIT 200',(base_id,))]
        result=[]
        for identifier in ids:
            try:result.append(ingestion.present(ingestion.require(identifier)))
            except HTTPException:pass
        return result

    @router.post('/api/ingestions',status_code=202)
    def upload(base_id:str=Form(...,min_length=1,max_length=150),file:UploadFile=File(...),
               replace_document_id:str|None=Form(default=None,max_length=150),
               idempotency_key:str=Header(alias='Idempotency-Key',min_length=1,max_length=100,pattern=r'\S')):
        return ingestion.submit(base_id,file,idempotency_key,replace_document_id=replace_document_id)

    @router.get('/api/ingestions/{identifier}')
    def status(identifier:str):return ingestion.present(ingestion.require(identifier))

    @router.post('/api/ingestions/{identifier}/cancel')
    def cancel(identifier:str):return ingestion.cancel(identifier)

    @router.post('/api/ingestions/{identifier}/retry')
    def retry(identifier:str,body:Retry):return ingestion.retry(identifier,body.acknowledge_uncertain)

    @router.post('/api/ingestions/{identifier}/activate')
    def activate(identifier:str):return ingestion.activate(identifier)

    @router.patch('/api/admin/knowledge/bases/{identifier}/policy')
    def policy(identifier:str,body:Policy):
        access.admin();base=access.base(identifier)
        if base['kind']=='temporary':raise HTTPException(409,'临时库使用自动生效')
        with access.repository.connect() as db:
            if not db.execute('UPDATE knowledge_bases SET activation_policy=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',(body.activation_policy,now_iso(),identifier,body.expected_revision)).rowcount:raise HTTPException(409,'知识库已变化，请刷新')
        return access.base(identifier)

    app.include_router(router)
