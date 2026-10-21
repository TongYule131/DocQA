"""账号和知识库接口；旧文档接口通过同一权限层保护。"""
import hmac
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.product_access import (COOKIE, PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH,
                                cancel_user_jobs, digest, public_user, password_hash)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(StrictBody):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=128, repr=False)


class UserCreate(Login):
    # 登录兼容旧密码；新设和重设密码采用当前长度规则。
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH, repr=False)
    display_name: str = Field(min_length=1, max_length=80)
    role: Literal['admin', 'customer'] = 'customer'


class UserUpdate(StrictBody):
    active: bool


class PasswordReset(StrictBody):
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH, repr=False)


class BaseCreate(StrictBody):
    name: str = Field(min_length=1, max_length=80, pattern=r'\S')
    description: str = Field(default='', max_length=1000)
    collection_name: str | None = Field(default=None, min_length=2, max_length=64, pattern=r'^[a-z0-9]+$')
    embedding_model: str | None = Field(default=None, max_length=150)
    chunk_max_chars: int = Field(default=800, ge=200, le=4000)
    chunk_overlap_chars: int = Field(default=120, ge=0, le=1000)


class Publish(StrictBody):
    expected_revision: int = Field(ge=0)


class Binding(StrictBody):
    document_id: str = Field(min_length=1, max_length=100)


class Member(StrictBody):
    enabled: bool


def authorize_request(access, request):
    """采用允许清单；新增旧接口不会因为忘记检查而默认向客户开放。"""
    actor, path, method = access.actor, request.url.path, request.method
    if path.startswith('/api/auth/'):
        return
    if path.startswith(('/api/kb-conversations','/api/kb-tasks/')) or path=='/app/knowledge':
        # 库级服务逐对象核对归属、发布快照和当前授权。
        return
    if path == '/api/conversations' or path.startswith('/api/conversations/'):
        # 对象归属由会话服务逐项检查；管理员不默认获得客户私有会话。
        return
    if path.startswith(('/api/admin/', '/admin/')) or path in ('/admin', '/tools', '/knowledge', '/docs', '/redoc', '/openapi.json'):
        access.admin(); return
    if path.startswith('/api/knowledge-bases'):
        if method != 'GET':
            access.admin()
        return
    match = re.fullmatch(r'/api/question-jobs/([^/]+)(?:/(cancel|retry))?', path)
    if match:
        access.require_question(match[1]); return
    match = re.fullmatch(r'/api/question-results/([^/]+)(?:/export)?', path)
    if match:
        with access.repository.connect() as db:
            row = db.execute('SELECT job_id FROM question_results WHERE id=?', (match[1],)).fetchone()
        if not row:
            raise HTTPException(404, '问答结果不存在或未获授权')
        access.require_question(row[0]); return
    if actor['role'] == 'admin':
        return
    match = re.fullmatch(r'/api/parse-tasks/([^/]+)', path)
    if method == 'GET' and match:
        task = access.repository.get_task(match[1])
        if task and access.attachment(task.document_id):
            return
    if (method == 'GET' and path in ('/app', '/', '/single', '/api/documents', '/api/rag/status')) or path.startswith('/static/'):
        return
    match = re.fullmatch(r'/api/documents/([^/]+)(?:/(original|content|chunks|index|versions|question-jobs))?', path)
    if match and (method == 'GET' or (method == 'POST' and match[2] == 'question-jobs')):
        access.require_document(match[1]); return
    raise HTTPException(403, '该操作需要管理员权限')


def protect_temporary_request(access, request):
    """旧入口也不能绕过会话范围；管理员不默认拥有客户临时原件。"""
    match = re.fullmatch(r'/api/documents/([^/]+)(?:/.*)?', request.url.path)
    if match and access.attachment(match[1]) and request.method != 'GET':
        raise HTTPException(403, '临时资料请在所属会话准备或提问')
    match = re.fullmatch(r'/api/parse-tasks/([^/]+)(?:/.*)?', request.url.path)
    if match:
        task = access.repository.get_task(match[1])
        if task and access.attachment(task.document_id) and request.method != 'GET':
            raise HTTPException(403, '临时资料请在所属会话操作')


def install_product_routes(app, access):
    router = APIRouter()

    @router.get('/api/auth/me')
    def me():
        return {'team_mode': access.team_mode,
                'demo_providers': bool(getattr(app.state, 'demo_providers', False)),
                'user': {k: v for k, v in access.actor.items() if k != 'csrf_token'} if access.actor else None,
                'csrf_token': access.actor.get('csrf_token', '') if access.actor else ''}

    @router.post('/api/auth/login')
    def login(body: Login, request: Request, response: Response):
        if not access.team_mode:
            raise HTTPException(409, '当前为本地单用户模式，无需登录')
        user, token, csrf = access.login(body.username.strip(), body.password,
                                         request.client.host if request.client else 'unknown')
        old = request.cookies.get(COOKIE)
        if old:
            with access.repository.connect() as db:
                db.execute('DELETE FROM product_sessions WHERE token_hash=?', (digest(old),))
        response.set_cookie(COOKIE, token, max_age=12 * 3600, secure=access.cookie_secure,
                            httponly=True, samesite='strict', path='/')
        return {'user': user, 'csrf_token': csrf}

    @router.post('/api/auth/logout')
    def logout(request: Request, response: Response):
        with access.repository.connect() as db:
            db.execute('DELETE FROM product_sessions WHERE token_hash=?', (digest(request.cookies.get(COOKIE, '')),))
        response.delete_cookie(COOKIE, path='/', secure=access.cookie_secure, httponly=True, samesite='strict')
        return {'status': 'signed_out'}

    @router.get('/api/admin/users')
    def users():
        access.admin()
        with access.repository.connect() as db:
            return [{**public_user(r), 'created_at': r['created_at']} for r in db.execute('SELECT * FROM product_users ORDER BY created_at')]

    @router.post('/api/admin/users', status_code=201)
    def create_user(body: UserCreate):
        access.admin()
        if not access.team_mode:
            raise HTTPException(409, '请先在本机 /setup 设置首位管理员并启用团队登录')
        return access.create_user(body.username, body.display_name, body.password, body.role)

    @router.patch('/api/admin/users/{user_id}')
    def update_user(user_id: str, body: UserUpdate):
        access.admin()
        with access.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT * FROM product_users WHERE id=?', (user_id,)).fetchone()
            if not user:
                raise HTTPException(404, '账号不存在')
            if not body.active and user['role'] == 'admin' and user['active']:
                if db.execute("SELECT COUNT(*) FROM product_users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                    raise HTTPException(409, '不能停用最后一个管理员')
            db.execute('UPDATE product_users SET active=? WHERE id=?', (int(body.active), user_id))
            if not body.active:
                db.execute('DELETE FROM product_sessions WHERE user_id=?', (user_id,))
                cancel_user_jobs(db, user_id)
            return public_user(db.execute('SELECT * FROM product_users WHERE id=?', (user_id,)).fetchone())

    @router.post('/api/admin/users/{user_id}/password')
    def reset_password(user_id: str, body: PasswordReset):
        access.admin()
        encoded = password_hash(body.password)
        with access.repository.connect() as db:
            if not db.execute('SELECT 1 FROM product_users WHERE id=?', (user_id,)).fetchone():
                raise HTTPException(404, '账号不存在')
            db.execute('UPDATE product_users SET password_hash=? WHERE id=?', (encoded, user_id))
            db.execute('DELETE FROM product_sessions WHERE user_id=?', (user_id,))
        return {'status': 'password_reset', 'sessions_revoked': True}

    @router.get('/api/knowledge-bases')
    def bases():
        return access.list_bases()

    @router.post('/api/knowledge-bases', status_code=201)
    def create_base(body: BaseCreate):
        return access.create_base(body.name, body.description, **body.model_dump(exclude={'name','description'}))

    @router.get('/api/knowledge-bases/{base_id}')
    def base_detail(base_id: str):
        return access.base_detail(base_id)

    @router.post('/api/knowledge-bases/{base_id}/documents')
    def bind(base_id: str, body: Binding):
        return access.bind_document(base_id, body.document_id)

    @router.delete('/api/knowledge-bases/{base_id}/documents/{document_id}')
    def unbind(base_id: str, document_id: str):
        return access.bind_document(base_id, document_id, remove=True)

    @router.post('/api/knowledge-bases/{base_id}/publish')
    def publish(base_id: str, body: Publish):
        return access.publish(base_id, body.expected_revision)

    @router.put('/api/knowledge-bases/{base_id}/members/{user_id}')
    def membership(base_id: str, user_id: str, body: Member):
        return access.set_member(base_id, user_id, body.enabled)

    app.include_router(router)


def verify_mutation(request, actor):
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '请求来源不一致，请从当前站点操作')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, '拒绝跨站请求')
    if actor and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), actor['csrf_token']):
        raise HTTPException(403, '登录校验已失效，请刷新后重试')
