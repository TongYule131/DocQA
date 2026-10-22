"""后台概览与本机首次设置。配置展示不探测供应商、不回传密钥。"""
import ipaddress
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from app.product_access import COOKIE, PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH, password_hash
from app.product_routes import BaseCreate, StrictBody, verify_mutation
from app.repository import now_iso


class Setup(StrictBody):
    username: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH, repr=False)


def local_setup_request(request):
    # 不信任转发头；同时约束直连来源和 Host，防止局域网抢占及 DNS rebinding。
    try:
        peer = ipaddress.ip_address(request.client.host)
        host = request.url.hostname
        local_host = host == 'localhost' or ipaddress.ip_address(host).is_loopback
    except (ValueError, AttributeError, TypeError):
        return False
    return peer.is_loopback and local_host


def safe_endpoint(value):
    """URL 中即使误配了凭证、查询参数或密钥路径，也只展示协议与主机。"""
    try:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname:
            return '地址格式无效'
        host = f'[{url.hostname}]' if ':' in url.hostname else url.hostname
        return f'{url.scheme}://{host}' + (f':{url.port}' if url.port else '')
    except ValueError:
        return '地址格式无效'


def install_admin_routes(app, access):
    router = APIRouter()

    @router.get('/api/auth/setup-status')
    def setup_status(request: Request):
        with access.repository.connect() as db:
            available = db.execute('SELECT COUNT(*) FROM product_users').fetchone()[0] == 0
        return {'available': available and local_setup_request(request), 'initialized': not available,
                'team_mode': access.team_mode}

    @router.post('/api/auth/setup', status_code=201)
    def setup(body: Setup, request: Request, response: Response):
        if not local_setup_request(request):
            raise HTTPException(403, '首次管理员只能在服务器本机设置')
        verify_mutation(request, None)
        with access.repository.connect() as db:
            if db.execute('SELECT 1 FROM product_users LIMIT 1').fetchone():
                raise HTTPException(409, '已完成账号初始化，请使用现有管理员登录')
        username, display = body.username.strip(), body.display_name.strip()
        if not 2 <= len(username) <= 64 or not all(c.isalnum() or c in '._-' for c in username) or not display:
            raise HTTPException(422, '账号或显示名称格式无效')
        encoded, identifier = password_hash(body.password), f'user-{uuid4().hex}'
        with access.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM product_users LIMIT 1').fetchone():
                raise HTTPException(409, '已完成账号初始化，请使用现有管理员登录')
            db.execute('INSERT INTO product_users VALUES (?,?,?,?,?,1,?)',
                       (identifier, username, display, encoded, 'admin', now_iso()))
            # 本地试用已有的私有会话只交给首位管理员，不能丢失，也不向新客户开放。
            db.execute('UPDATE conversations SET owner_user_id=? WHERE owner_user_id IS NULL', (identifier,))
            db.execute('UPDATE kb_conversations SET owner_user_id=? WHERE owner_user_id IS NULL',(identifier,))
            db.execute('UPDATE kb_tasks SET owner_user_id=? WHERE owner_user_id IS NULL',(identifier,))
            db.execute('UPDATE conversation_attachments SET owner_user_id=? WHERE owner_user_id IS NULL', (identifier,))
            db.execute("UPDATE knowledge_bases SET created_by=? WHERE created_by IS NULL AND kind='temporary'",(identifier,))
            db.execute('UPDATE ingestion_jobs SET owner_user_id=? WHERE owner_user_id IS NULL',(identifier,))
            db.execute('UPDATE chat_submissions SET owner_user_id=? WHERE owner_user_id IS NULL',(identifier,))
            db.execute('UPDATE question_jobs SET owner_user_id=? WHERE owner_user_id IS NULL AND '
                       'conversation_id IN (SELECT id FROM conversations WHERE owner_user_id=?)', (identifier, identifier))
            db.execute('UPDATE product_installation SET team_enabled=1,cookie_secure=?,initialized_at=? WHERE singleton=1',
                       (int(request.url.scheme == 'https'), now_iso()))
        user, token, csrf = access.login(username, body.password, request.client.host)
        response.set_cookie(COOKIE, token, max_age=12 * 3600, secure=access.cookie_secure,
                            httponly=True, samesite='strict', path='/')
        return {'user': user, 'csrf_token': csrf, 'team_mode': True}

    @router.get('/api/admin/overview')
    def overview():
        access.admin()
        with access.repository.connect() as db:
            bases = [dict(row) for row in db.execute('''SELECT k.*,
                COALESCE(u.display_name,'本地导入') AS owner_name,
                (SELECT COUNT(*) FROM knowledge_base_documents d WHERE d.knowledge_base_id=k.id) AS document_count,
                (SELECT COUNT(*) FROM knowledge_base_members m WHERE m.knowledge_base_id=k.id) AS member_count
                FROM knowledge_bases k LEFT JOIN product_users u ON u.id=k.created_by ORDER BY k.created_at DESC''')]
            documents = db.execute('SELECT COUNT(*) FROM documents d WHERE NOT EXISTS '
                                   '(SELECT 1 FROM conversation_attachments a WHERE a.document_id=d.id)').fetchone()[0]
            creators = db.execute('SELECT COUNT(DISTINCT created_by) FROM knowledge_bases').fetchone()[0]
            users = db.execute('SELECT COUNT(*) FROM product_users').fetchone()[0]
        allowed={b['id'] for b in access.list_bases()};bases=[b for b in bases if b['id'] in allowed]
        documents=len(access.visible_documents(access.repository.list_documents()))
        return {'bases': bases, 'stats': {'knowledge_bases': len(bases), 'documents': documents,
                'populated_bases': sum(b['document_count'] > 0 for b in bases), 'creators': creators, 'users': users},
                'index_storage': 'SQLite · 每份文档独立索引', 'embedding_model': access.settings.embedding_model,
                'team_mode': access.team_mode}

    @router.patch('/api/knowledge-bases/{base_id}')
    def edit_base(base_id: str, body: BaseCreate):
        access.admin(); access.base(base_id)
        with access.repository.connect() as db:
            db.execute('UPDATE knowledge_bases SET name=?,description=?,updated_at=? WHERE id=?',
                       (body.name.strip(), body.description.strip(), now_iso(), base_id))
        return access.base(base_id)

    @router.delete('/api/knowledge-bases/{base_id}')
    def delete_empty_base(base_id: str):
        access.admin()
        with access.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM knowledge_bases WHERE id=?', (base_id,)).fetchone():
                raise HTTPException(404, '知识库不存在')
            if db.execute('SELECT 1 FROM knowledge_base_documents WHERE knowledge_base_id=?', (base_id,)).fetchone() or db.execute(
                    'SELECT 1 FROM knowledge_base_snapshots WHERE knowledge_base_id=?', (base_id,)).fetchone():
                raise HTTPException(409, '仅可删除无文档且从未发布的空知识库；已有发布历史保留供追溯')
            db.execute('DELETE FROM knowledge_base_members WHERE knowledge_base_id=?', (base_id,))
            db.execute('DELETE FROM knowledge_bases WHERE id=?', (base_id,))
        return {'status': 'deleted'}

    @router.get('/api/admin/settings')
    def settings():
        access.admin(); s = access.settings
        return {'team_mode': access.team_mode, 'cookie_secure': access.cookie_secure,
                'generation': {'provider': 'DeepSeek', 'model': s.deepseek_model,
                    'endpoint': safe_endpoint(s.deepseek_base_url), 'configured': bool(s.deepseek_api_key.strip()),
                    'thinking': s.deepseek_thinking, 'reasoning_effort': s.deepseek_reasoning_effort},
                'embedding': {'model': s.embedding_model, 'endpoint': safe_endpoint(s.embedding_base_url),
                    'configured': bool(s.embedding_api_key.strip()), 'batch_size': s.embedding_batch_size},
                'parsing': {'endpoint': safe_endpoint(s.docling_base_url), 'ocr_engine': s.docling_ocr_engine,
                    'ocr_lang': s.docling_ocr_lang, 'max_pdf_pages': s.max_pdf_pages},
                'limits': {'max_upload_mb': s.max_upload_mb, 'attachment_ttl_hours': s.attachment_ttl_hours,
                    'attachment_max_active': s.attachment_max_active},
                'configuration_source': '服务器环境配置；修改后须完整重启服务，已有索引不会自动重建。'}

    app.include_router(router)
