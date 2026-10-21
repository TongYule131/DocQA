from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from app.product_routes import StrictBody


class ConversationCreate(StrictBody):
    document_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=100)


class ConversationUpdate(StrictBody):
    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=80, pattern=r'\S')
    memory_enabled: bool | None = None
    memory_notes: str | None = Field(default=None, max_length=800)
    clear_memory: bool = False
    archived: bool | None = None
    deleted: bool = False


class TurnCreate(StrictBody):
    question: str = Field(min_length=1, max_length=4000, pattern=r'\S')
    idempotency_key: str = Field(min_length=1, max_length=100)


def install_conversation_routes(app, service):
    router = APIRouter(prefix='/api/conversations')

    @router.get('')
    def conversations(archived: bool = False):
        return service.list(archived)

    @router.post('', status_code=201)
    def create(body: ConversationCreate):
        return service.create(body.document_id, body.idempotency_key)

    @router.get('/{identifier}')
    def detail(identifier: str):
        return service.detail(identifier)

    @router.patch('/{identifier}')
    def update(identifier: str, body: ConversationUpdate):
        return service.update(identifier, body)

    @router.post('/{identifier}/turns')
    def turn(identifier: str, body: TurnCreate, request: Request):
        result, code = service.submit(identifier, body, request.state.request_id)
        return JSONResponse(result, status_code=code)

    app.include_router(router)
