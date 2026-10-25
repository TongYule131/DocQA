"""团队模式的身份、对象权限与知识库发布；网络请求不持有数据库写事务。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextvars import ContextVar
from uuid import uuid4

from fastapi import HTTPException

from app.repository import now_iso

actor_context: ContextVar[dict | None] = ContextVar('docqa_actor', default=None)
COOKIE = 'docqa_session'
ITERATIONS = 600_000
PASSWORD_MIN_LENGTH = 4
PASSWORD_MAX_LENGTH = 32


def password_hash(password: str) -> str:
    if not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise HTTPException(422, '密码须为4～32个字符')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or int(iterations) != ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def public_user(row) -> dict:
    return {key: row[key] for key in ('id', 'username', 'display_name', 'role', 'active')}


def cancel_user_jobs(db, user_id: str, base_id: str | None = None):
    """撤权只停止后续处理；已发出的请求记为不确定，不承诺上游停止计费。"""
    clause, params = ('', (user_id,)) if base_id is None else (
        ' AND knowledge_snapshot_id IN (SELECT id FROM knowledge_base_snapshots WHERE knowledge_base_id=?)', (user_id, base_id))
    jobs = db.execute("SELECT id FROM question_jobs WHERE owner_user_id=? AND status IN ('queued','running')" + clause, params).fetchall()
    for row in jobs:
        stamp = now_iso()
        db.execute("UPDATE question_calls SET status='uncertain',error_code='access_revoked',settled_at=? WHERE job_id=? AND status='intent'", (stamp, row[0]))
        db.execute("UPDATE question_jobs SET status='cancelled',cancel_requested=1,error_code='access_revoked',"
                   "error_message='资料权限已撤销，停止后续处理；在途请求可能继续计费',finished_at=?,updated_at=?,"
                   "lease_token=NULL,lease_expires_at=NULL WHERE id=?", (stamp, stamp, row[0]))
        db.execute("UPDATE question_attempts SET status='cancelled',finished_at=?,error_code='access_revoked' WHERE job_id=? AND status='running'", (stamp, row[0]))
    if base_id is None:
        stamp=now_iso()
        db.execute("UPDATE analysis_calls SET status='uncertain',error_code='access_revoked',settled_at=? WHERE status='intent' AND job_id IN (SELECT id FROM analysis_jobs WHERE owner_user_id=?)",(stamp,user_id))
        db.execute("UPDATE analysis_jobs SET status='cancelled',cancel_requested=1,lease_token=NULL,lease_expires_at=NULL,error_code='access_revoked',error_message='账号已停用，停止后续处理',updated_at=? WHERE owner_user_id=? AND status IN ('queued','running')",(stamp,user_id))
        db.execute("UPDATE parse_tasks SET status='needs_attention',cancel_requested=1,lease_token=NULL,lease_expires_at=NULL,error_code='access_revoked',error_message='账号已停用，请人工核对解析状态',updated_at=? WHERE owner_user_id=? AND status IN ('queued','running')",(stamp,user_id))


class ProductAccess:
    def __init__(self, repository, settings, index):
        self.repository, self.settings, self.index = repository, settings, index
        repository.settings=settings

    @property
    def actor(self):
        return actor_context.get()

    @property
    def team_mode(self):
        if self.settings.team_mode:
            return True
        with self.repository.connect() as db:
            return bool(db.execute('SELECT team_enabled FROM product_installation WHERE singleton=1').fetchone()[0])

    @property
    def cookie_name(self):
        import re
        value=self.settings.session_cookie_name
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):raise ValueError('invalid_cookie_name')
        return value

    @property
    def cookie_secure(self):
        if self.settings.team_mode:
            return self.settings.session_cookie_secure
        with self.repository.connect() as db:
            return bool(db.execute('SELECT cookie_secure FROM product_installation WHERE singleton=1').fetchone()[0])

    def admin(self):
        if self.team_mode and (not self.actor or self.actor['role'] != 'admin'):
            raise HTTPException(403, '该操作需要管理员权限')

    def create_user(self, username, display_name, password, role='customer'):
        username = username.strip()
        if not 2 <= len(username) <= 64 or not all(c.isalnum() or c in '._-' for c in username):
            raise HTTPException(422, '账号须为2～64个字母、数字或 ._- 字符')
        if role not in ('admin', 'customer') or not display_name.strip() or len(display_name) > 80:
            raise HTTPException(422, '账号资料无效')
        encoded, identifier = password_hash(password), f'user-{uuid4().hex}'
        try:
            with self.repository.connect() as db:
                db.execute('INSERT INTO product_users VALUES (?,?,?,?,?,1,?)',
                           (identifier, username, display_name.strip(), encoded, role, now_iso()))
                return public_user(db.execute('SELECT * FROM product_users WHERE id=?', (identifier,)).fetchone())
        except sqlite3.IntegrityError:
            raise HTTPException(409, '账号已存在') from None

    def login(self, username: str, password: str, peer: str):
        now = int(time.time())
        # 同时限制账号和来源，防止简单更换用户名或来源绕过。不存在账号同样执行慢哈希。
        keys = ['user:' + digest(username.casefold()), 'peer:' + digest(peer)]
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM product_login_limits WHERE window_start<?', (now - 600,))
            for key in keys:
                row = db.execute('SELECT * FROM product_login_limits WHERE key=?', (key,)).fetchone()
                maximum = 5 if key.startswith('user:') else 30
                if row and row['failures'] >= maximum and now - row['window_start'] < 300:
                    raise HTTPException(429, '登录尝试过多，请5分钟后重试')
                db.execute('INSERT INTO product_login_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET '
                           'failures=CASE WHEN window_start<? THEN 1 ELSE failures+1 END,'
                           'window_start=CASE WHEN window_start<? THEN excluded.window_start ELSE window_start END',
                           (key, now, now - 300, now - 300))
            user = db.execute('SELECT * FROM product_users WHERE username=? COLLATE NOCASE', (username,)).fetchone()
        encoded = user['password_hash'] if user else f'pbkdf2_sha256${ITERATIONS}${"00" * 16}${"00" * 32}'
        valid = verify_password(password, encoded)
        if not valid or not user or not user['active']:
            raise HTTPException(401, '账号或密码不正确，或账号已停用')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT active,password_hash FROM product_users WHERE id=?', (user['id'],)).fetchone()
            if not current or not current['active'] or current['password_hash'] != encoded:
                raise HTTPException(401, '账号状态已变更，请重新登录')
            # 成功仅清账号失败计数；来源限流仍限制高频认证请求。
            db.execute('DELETE FROM product_login_limits WHERE key=?', (keys[0],))
            db.execute('DELETE FROM product_sessions WHERE expires_at<=?', (now,))
            db.execute('INSERT INTO product_sessions VALUES (?,?,?,?,?)',
                       (digest(token), user['id'], csrf, now + 12 * 3600, now_iso()))
        return public_user(user), token, csrf

    def session(self, token):
        if not token or len(token) > 200:
            return None
        with self.repository.connect() as db:
            row = db.execute('SELECT u.*,s.csrf_token FROM product_sessions s JOIN product_users u ON u.id=s.user_id '
                             'WHERE s.token_hash=? AND s.expires_at>? AND u.active=1',
                             (digest(token), int(time.time()))).fetchone()
        return {**public_user(row), 'csrf_token': row['csrf_token']} if row else None

    def attachment(self, document_id, db=None):
        if db is None:
            with self.repository.connect() as connection:
                return self.attachment(document_id, connection)
        row = db.execute('SELECT * FROM conversation_attachments WHERE document_id=?', (document_id,)).fetchone()
        if row:
            from app.attachment_schema import attachment_available
            owner = self.actor['id'] if self.team_mode else None
            if row['owner_user_id'] != owner:
                raise HTTPException(404, '资料不存在或未获授权')
            if not attachment_available(db, document_id):
                raise HTTPException(410, '临时资料已到期或移除，原回答和来源不再提供读取；请新建会话上传')
        return dict(row) if row else None

    def require_document(self, document_id: str):
        attachment=self.attachment(document_id)
        # 新临时库严格按所有人隔离；管理员也不能从旧文档接口绕过。
        from app.knowledge_scope import base_allowed, scope_documents
        owner=self.actor['id'] if self.team_mode else None
        with self.repository.connect() as db:
            private=db.execute("SELECT k.id FROM knowledge_bases k WHERE k.kind='temporary' AND (EXISTS(SELECT 1 FROM knowledge_base_documents d WHERE d.knowledge_base_id=k.id AND d.document_id=?) OR EXISTS(SELECT 1 FROM ingestion_jobs j WHERE j.base_id=k.id AND j.document_id=?))",(document_id,document_id)).fetchone()
            if private:
                if not base_allowed(db,private[0],owner):raise HTTPException(404,'资料不存在、已到期或未获授权')
                if document_id not in scope_documents(db,private[0]) and not db.execute('SELECT 1 FROM knowledge_base_documents WHERE knowledge_base_id=? AND document_id=?',(private[0],document_id)).fetchone():raise HTTPException(404,'资料已移除')
                published=db.execute('SELECT s.* FROM knowledge_base_snapshot_documents s JOIN knowledge_base_snapshots k ON k.id=s.snapshot_id WHERE k.knowledge_base_id=? AND s.document_id=? ORDER BY k.revision DESC LIMIT 1',(private[0],document_id)).fetchone()
                return dict(published) if published else None
        if attachment:return None
        if not self.team_mode or self.actor['role'] == 'admin':
            return None
        with self.repository.connect() as db:
            row = db.execute('SELECT sd.* FROM knowledge_base_snapshot_documents sd '
                'JOIN knowledge_bases k ON k.active_snapshot_id=sd.snapshot_id '
                'JOIN knowledge_base_members m ON m.knowledge_base_id=k.id '
                'WHERE sd.document_id=? AND m.user_id=?', (document_id, self.actor['id'])).fetchone()
        if row is None:
            # 同一资料的新版本替换保留旧引用；移除当前版本仍立即撤销整条版本链。
            with self.repository.connect() as db:
                for (base_id,) in db.execute('SELECT knowledge_base_id FROM knowledge_base_members WHERE user_id=?',(owner,)):
                    if base_allowed(db,base_id,owner) and document_id in scope_documents(db,base_id):
                        row=db.execute('SELECT s.* FROM knowledge_base_snapshot_documents s JOIN knowledge_base_snapshots k ON k.id=s.snapshot_id WHERE k.knowledge_base_id=? AND s.document_id=? ORDER BY k.revision DESC LIMIT 1',(base_id,document_id)).fetchone()
                        if row:break
        if row is None:
            raise HTTPException(404, '资料不存在或未获授权')
        return dict(row)

    def visible_documents(self, documents):
        with self.repository.connect() as db:
            temporary = {r[0] for r in db.execute('SELECT document_id FROM conversation_attachments')}
        documents = [d for d in documents if d.id not in temporary]
        if not self.team_mode or self.actor['role'] == 'admin':
            result=[]
            for d in documents:
                try:self.require_document(d.id);result.append(d)
                except HTTPException:pass
            return result
        with self.repository.connect() as db:
            ids = {r[0] for r in db.execute('SELECT sd.document_id FROM knowledge_base_snapshot_documents sd '
                'JOIN knowledge_bases k ON k.active_snapshot_id=sd.snapshot_id '
                'JOIN knowledge_base_members m ON m.knowledge_base_id=k.id WHERE m.user_id=?', (self.actor['id'],))}
        return [self.present_document(d) for d in documents if d.id in ids]

    def present_document(self, document):
        published = self.require_document(document.id)
        if not published:
            return document
        version = self.repository.get_parse_version(published['parse_version_id'])
        return document.model_copy(update={
            'active_parse_version_id': published['parse_version_id'], 'active_index_id': published['index_id'],
            'page_count': version.page_count, 'chunk_count': version.chunk_count, 'status': 'parsed',
            'latest_task_id': None, 'task_status': None, 'latest_task_error': None,
            'task_stage': None, 'error': None, 'quality_status': version.quality_status, 'index_version_mismatch': False})

    def published_index_info(self, published):
        from app.vector_index import evidence_signature
        with self.repository.connect() as db:
            row = db.execute('SELECT * FROM embedding_indexes WHERE id=? AND document_id=?',
                             (published['index_id'], published['document_id'])).fetchone()
        if not row or row['source_signature'] != published['source_signature']:
            raise HTTPException(409, '知识库发布索引不可用，请联系管理员')
        info = self.index.verify_snapshot(published['document_id'], {
            'index_id': published['index_id'], 'parse_version_id': published['parse_version_id'],
            'source_signature': row['source_signature'], 'source_signature_algo': row['source_signature_algo'],
            'provider_signature': row['provider_signature'], 'evidence_signature': evidence_signature(
                self.repository.chunks(published['document_id'], published['parse_version_id']))})
        return {**info, 'dimension': row['dimension'], 'chunk_count': row['chunk_count'], 'error': None, 'matches_active_version': True}

    def require_question(self, job_id):
        with self.repository.connect() as db:
            row = db.execute('SELECT * FROM question_jobs WHERE id=?', (job_id,)).fetchone()
            if row and row['conversation_id']:
                conv = db.execute('SELECT * FROM conversations WHERE id=?', (row['conversation_id'],)).fetchone()
                if not conv or conv['deleted_at']:
                    raise HTTPException(404, '问答记录不存在或未获授权')
        if not self.team_mode:
            if row:
                self.require_document(row['document_id'])
            return
        # 管理员也不能以诊断名义读取其他用户的私有问题和回答。
        if row is None or (row['owner_user_id'] != self.actor['id'] and
                           not (row['owner_user_id'] is None and self.actor['role'] == 'admin')):
            raise HTTPException(404, '问答记录不存在或未获授权')
        self.require_document(row['document_id'])

    def list_bases(self):
        from app.knowledge_scope import base_allowed
        owner=self.actor['id'] if self.team_mode else None
        with self.repository.connect() as db:
            rows=db.execute('SELECT * FROM knowledge_bases ORDER BY created_at DESC').fetchall()
            return [dict(r) for r in rows if base_allowed(db,r['id'],owner) and
                    (owner is None or self.actor['role']=='admin' or r['kind']=='temporary' or r['active_snapshot_id'])]

    def base(self, identifier):
        for item in self.list_bases():
            if item['id'] == identifier:
                return item
        raise HTTPException(404, '知识库不存在或未获授权')

    def base_detail(self, identifier):
        result = self.base(identifier)
        is_admin = not self.team_mode or self.actor['role'] == 'admin'
        with self.repository.connect() as db:
            if is_admin or result['kind']=='temporary':
                documents = db.execute('SELECT document_id FROM knowledge_base_documents WHERE knowledge_base_id=?', (identifier,)).fetchall()
                result['members'] = [r[0] for r in db.execute('SELECT user_id FROM knowledge_base_members WHERE knowledge_base_id=?', (identifier,))]
                result['snapshots'] = [dict(r) for r in db.execute('SELECT * FROM knowledge_base_snapshots WHERE knowledge_base_id=? ORDER BY revision DESC', (identifier,))]
            else:
                documents = db.execute('SELECT document_id FROM knowledge_base_snapshot_documents WHERE snapshot_id=?', (result['active_snapshot_id'],)).fetchall()
            result['published_documents'] = [dict(r) for r in db.execute('SELECT * FROM knowledge_base_snapshot_documents WHERE snapshot_id=?', (result['active_snapshot_id'],))]
        result['documents'] = [self.present_document(self.repository.get(r[0])).model_dump() for r in documents if self.repository.get(r[0])]
        return result

    def create_base(self, name, description, *, collection_name=None, embedding_model=None, chunk_max_chars=800, chunk_overlap_chars=120, activation_policy='auto'):
        self.admin()
        identifier = f'kb-{uuid4().hex}'
        if embedding_model and embedding_model!=self.settings.embedding_model:
            raise HTTPException(422,'请选择系统设置中已配置的向量模型')
        if chunk_overlap_chars>=chunk_max_chars:
            raise HTTPException(422,'重叠字符数必须小于分块上限')
        collection_name=collection_name or identifier.replace('-','')
        with self.repository.connect() as db:
            stamp = now_iso()
            if db.execute('SELECT 1 FROM knowledge_bases WHERE collection_name=?',(collection_name,)).fetchone():
                raise HTTPException(409,'存储标识已被使用')
            db.execute('INSERT INTO knowledge_bases(id,name,description,created_by,created_at,updated_at,collection_name,embedding_signature,embedding_model,chunk_max_chars,chunk_overlap_chars,activation_policy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier, name.strip(), description.strip(), self.actor['id'] if self.actor else None, stamp, stamp,collection_name,self.index.embedding.signature,self.settings.embedding_model,chunk_max_chars,chunk_overlap_chars,activation_policy))
        return self.base(identifier)

    def bind_document(self, base_id, document_id, *, remove=False):
        self.admin(); self.base(base_id)
        with self.repository.connect() as db:
            if db.execute('SELECT 1 FROM conversation_attachments WHERE document_id=?', (document_id,)).fetchone():
                raise HTTPException(409, '临时资料不能加入知识库；如需长期保存，请由管理员重新导入')
        self.require_document(document_id)
        if not self.repository.get(document_id):
            raise HTTPException(404, '文档不存在')
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if remove:
                db.execute('DELETE FROM knowledge_base_documents WHERE knowledge_base_id=? AND document_id=?', (base_id, document_id))
            else:
                current = db.execute('SELECT knowledge_base_id FROM knowledge_base_documents WHERE document_id=?', (document_id,)).fetchone()
                if current and current[0] != base_id:
                    raise HTTPException(409, '文档已属于其他知识库；请先从原库移除')
                db.execute('INSERT OR IGNORE INTO knowledge_base_documents VALUES (?,?,?)', (base_id, document_id, now_iso()))
            db.execute('UPDATE knowledge_bases SET updated_at=? WHERE id=?', (now_iso(), base_id))
        return self.base_detail(base_id)

    def publish(self, base_id, expected_revision):
        self.admin(); base = self.base_detail(base_id)
        if not base['documents']:
            raise HTTPException(409, '空知识库不能发布')
        frozen = []
        for document in base['documents']:
            info = self.index.index_info(document['id'])
            if info['status'] != 'indexed' or not info['matches_active_version']:
                raise HTTPException(409, f'请先完成“{document["filename"]}”的解析检查与索引')
            version = self.repository.get_parse_version(info['parse_version_id'])
            active = self.repository.active_index(document['id'])
            if not version or version.quality_status == 'invalid' or not active:
                raise HTTPException(409, '资料版本不可发布')
            frozen.append((document['id'], version.id, active.id, version.origin_hash, active.source_signature))
        identifier = f'kbs-{uuid4().hex}'
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revision FROM knowledge_bases WHERE id=?', (base_id,)).fetchone()
            current_docs = {r[0] for r in db.execute('SELECT document_id FROM knowledge_base_documents WHERE knowledge_base_id=?', (base_id,))}
            if current['revision'] != expected_revision or current_docs != {r[0] for r in frozen}:
                raise HTTPException(409, '知识库已变更，请刷新后发布')
            # 锁内再次核对活动指针，防止检查与发布之间切换解析或索引版本。
            for doc, version, index_id, _, _ in frozen:
                actual = db.execute('SELECT active_parse_version_id,active_index_id FROM documents WHERE id=?', (doc,)).fetchone()
                if tuple(actual) != (version, index_id):
                    raise HTTPException(409, '资料版本已变更，请刷新后发布')
            revision = expected_revision + 1
            db.execute('INSERT INTO knowledge_base_snapshots VALUES (?,?,?,?,?)',
                       (identifier, base_id, revision, self.actor['id'] if self.actor else None, now_iso()))
            for item in frozen:
                db.execute('INSERT INTO knowledge_base_snapshot_documents VALUES (?,?,?,?,?,?)', (identifier, *item))
            db.execute('UPDATE knowledge_bases SET active_snapshot_id=?,revision=?,updated_at=? WHERE id=?', (identifier, revision, now_iso(), base_id))
        return self.base_detail(base_id)

    def set_member(self, base_id, user_id, enabled):
        self.admin(); self.base(base_id)
        with self.repository.connect() as db:
            if not db.execute('SELECT 1 FROM product_users WHERE id=? AND active=1', (user_id,)).fetchone():
                raise HTTPException(404, '账号不存在或已停用')
            if enabled:
                db.execute('INSERT OR IGNORE INTO knowledge_base_members VALUES (?,?,?)', (base_id, user_id, now_iso()))
            else:
                db.execute('DELETE FROM knowledge_base_members WHERE knowledge_base_id=? AND user_id=?', (base_id, user_id))
                cancel_user_jobs(db, user_id, base_id)
        return self.base_detail(base_id)
