"""临时附件只建立专属单资料会话，不加入知识库，也不隐式解析或建索引。"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, File, Header, HTTPException, UploadFile

from app.attachment_schema import attachment_available
from app.conversations import stop_conversation_jobs
from app.schemas import ParseRequest
from app.repository import now_iso
from app.uploads import store_upload


def install_attachment_routes(app, conversations, parse, build_index):
    router = APIRouter(prefix='/api/conversations')
    access, repository = conversations.access, conversations.repository

    @router.post('/temporary', status_code=201)
    def upload(file: UploadFile = File(...), key: str = Header(alias='Idempotency-Key', min_length=1, max_length=100)):
        owner = conversations.owner

        def link(db, document):
            previous = db.execute('SELECT * FROM conversation_attachments WHERE document_id=?', (document.id,)).fetchone()
            if previous:
                if not attachment_available(db, document.id):
                    raise HTTPException(410, '原临时附件已到期或移除，请用新请求重新上传')
                return
            if owner and not db.execute('SELECT 1 FROM product_users WHERE id=? AND active=1', (owner,)).fetchone():
                raise HTTPException(403, '账号已停用')
            count = db.execute('SELECT COUNT(*) FROM conversation_attachments WHERE owner_user_id IS ? '
                'AND expires_at>? AND revoked_at IS NULL AND purged_at IS NULL', (owner, now_iso())).fetchone()[0]
            if count >= access.settings.attachment_max_active:
                raise HTTPException(409, '临时资料数量已达上限，请先移除不再使用的附件')
            identifier, stamp = 'conv-' + uuid4().hex, now_iso()
            expires = (datetime.now(timezone.utc) + timedelta(hours=access.settings.attachment_ttl_hours)).isoformat()
            db.execute('INSERT INTO conversations(id,owner_user_id,document_id,title,request_key,created_at,updated_at) '
                       'VALUES (?,?,?,?,?,?,?)', (identifier, owner, document.id, document.filename[:80], 'temporary/' + key, stamp, stamp))
            db.execute('INSERT INTO conversation_attachments(document_id,conversation_id,owner_user_id,expires_at,created_at) '
                       'VALUES (?,?,?,?,?)', (document.id, identifier, owner, expires, stamp))

        result = store_upload(repository, access.settings, file, key, scope='attachment/' + (owner or 'local'), link=link)
        with repository.connect() as db:
            identifier = db.execute('SELECT conversation_id FROM conversation_attachments WHERE document_id=?', (result['document'].id,)).fetchone()[0]
        return conversations.detail(identifier)

    @router.post('/{identifier}/attachment/parse')
    def prepare(identifier: str, key: str = Header(alias='Idempotency-Key', min_length=1, max_length=100)):
        c = conversations.require(identifier, writable=True)
        require_attachment(c)
        return parse(c['document_id'], ParseRequest(idempotency_key=key), None)

    @router.post('/{identifier}/attachment/index')
    def index(identifier: str):
        c = conversations.require(identifier, writable=True)
        require_attachment(c)
        # 沿用索引候选租约；失败不自动重试，已就绪索引直接复用。
        return build_index(c['document_id'], False, None)

    def require_attachment(c):
        with repository.connect() as db:
            if not db.execute('SELECT 1 FROM conversation_attachments WHERE conversation_id=?', (c['id'],)).fetchone():
                raise HTTPException(409, '当前会话没有临时附件')

    @router.delete('/{identifier}/attachment')
    def remove(identifier: str):
        with repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            c = db.execute('SELECT * FROM conversations WHERE id=? AND owner_user_id IS ? AND deleted_at IS NULL', (identifier, conversations.owner)).fetchone()
            if not c:
                raise HTTPException(404, '会话不存在或未获授权')
            require_attachment(c)
            db.execute('UPDATE conversation_attachments SET revoked_at=COALESCE(revoked_at,?) WHERE conversation_id=?', (now_iso(), identifier))
            stop_conversation_jobs(db, identifier)
            db.execute("UPDATE parse_tasks SET cancel_requested=1 WHERE document_id=? AND status IN ('queued','running')", (c['document_id'],))
        return {'removed': True, 'detail': '已停止使用，原件与派生内容将在停机维护时清理；在途请求可能仍计费'}

    app.include_router(router)
