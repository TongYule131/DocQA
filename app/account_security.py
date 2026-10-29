"""个人资料、凭证和审计的原子更新；不保存口令正文或会话凭证。"""
from hashlib import sha256
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field

from app.repository import now_iso

INITIAL_PASSWORD='123456'


class NameUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    display_name:str=Field(min_length=1,max_length=80)


class PasswordChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    old_password:str=Field(min_length=1,max_length=128,repr=False)
    new_password:str=Field(min_length=4,max_length=32,repr=False)
    confirm_password:str=Field(min_length=4,max_length=32,repr=False)


def current_session(db,access):
    actor=access.actor
    if not actor:raise HTTPException(401,'请先登录')
    user=db.execute('SELECT u.*,COALESCE(a.must_change_password,0) AS must_change_password '
        'FROM product_users u LEFT JOIN product_account_state a ON a.user_id=u.id '
        'WHERE u.id=? AND u.active=1 AND EXISTS(SELECT 1 FROM product_sessions s '
        'WHERE s.user_id=u.id AND s.csrf_token=? AND s.expires_at>?)',
        (actor['id'],actor.get('csrf_token',''),int(time.time()))).fetchone()
    if not user:raise HTTPException(401,'登录已失效，请重新登录')
    return user


def event(db,actor,target,action):
    row=db.execute('SELECT credential_version FROM product_account_state WHERE user_id=?',(target,)).fetchone()
    db.execute('INSERT INTO product_account_events VALUES (?,?,?,?,?,?)',
        ('account-event-'+uuid4().hex,actor,target,action,row[0] if row else 0,now_iso()))


def update_credentials(db,user_id,encoded,*,forced):
    db.execute('UPDATE product_users SET password_hash=? WHERE id=?',(encoded,user_id))
    db.execute('INSERT INTO product_account_state VALUES (?,?,1,?) ON CONFLICT(user_id) DO UPDATE SET '
        'must_change_password=excluded.must_change_password,credential_version=credential_version+1,updated_at=excluded.updated_at',
        (user_id,int(forced),now_iso()))
    db.execute('DELETE FROM product_sessions WHERE user_id=?',(user_id,))


def change_name(access,name):
    name=name.strip()
    if not name:raise HTTPException(422,'显示名称不能为空')
    with access.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE');user=current_session(db,access)
        if user['must_change_password']:raise HTTPException(403,'请先修改初始密码')
        if user['display_name']!=name:
            db.execute('UPDATE product_users SET display_name=? WHERE id=?',(name,user['id']))
            event(db,user['id'],user['id'],'display_name_changed')


def change_password(access,body):
    from app.product_access import password_hash,verify_password
    if body.new_password!=body.confirm_password:raise HTTPException(422,'两次新密码不一致')
    with access.repository.connect() as db:user=current_session(db,access)
    key='password-change:'+sha256(user['id'].encode()).hexdigest();stamp=int(time.time())
    with access.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        limit=db.execute('SELECT failures,window_start FROM product_login_limits WHERE key=?',(key,)).fetchone()
        if limit and limit['failures']>=5 and stamp-limit['window_start']<300:raise HTTPException(429,'改密验证尝试过多，请5分钟后重试')
        db.execute('INSERT INTO product_login_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET '
            'failures=CASE WHEN window_start<? THEN 1 ELSE failures+1 END,window_start=CASE WHEN window_start<? THEN excluded.window_start ELSE window_start END',
            (key,stamp,stamp-300,stamp-300))
    # 慢哈希在写事务外进行；提交时比较原哈希和当前会话，阻止并发重置／登出穿透。
    if not verify_password(body.old_password,user['password_hash']):raise HTTPException(400,'原密码不正确')
    if verify_password(body.new_password,user['password_hash']):raise HTTPException(422,'新密码不能与原密码相同')
    encoded=password_hash(body.new_password)
    with access.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE');current=current_session(db,access)
        if current['password_hash']!=user['password_hash']:raise HTTPException(409,'密码已被其他操作修改，请重新登录后核对')
        update_credentials(db,user['id'],encoded,forced=False)
        event(db,user['id'],user['id'],'password_changed')
        db.execute('DELETE FROM product_login_limits WHERE key=?',(key,))


def reset_password(access,user_id):
    from app.product_access import password_hash
    access.admin();encoded=password_hash(INITIAL_PASSWORD)
    with access.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE');actor=current_session(db,access)
        if actor['role']!='admin' or actor['must_change_password']:raise HTTPException(403,'该操作需要已完成安全设置的管理员')
        if not db.execute('SELECT 1 FROM product_users WHERE id=?',(user_id,)).fetchone():raise HTTPException(404,'账号不存在')
        update_credentials(db,user_id,encoded,forced=True)
        event(db,actor['id'],user_id,'password_reset')


def restrict_password_change(request,actor):
    """强制改密状态在服务端限制；直接旧入口或管理员权限也不能绕过。"""
    if not actor or not actor.get('must_change_password'):return False
    allowed={('GET','/api/auth/me'),('POST','/api/auth/password'),('POST','/api/auth/logout'),('GET','/account/password')}
    return not request.url.path.startswith('/static/') and (request.method,request.url.path) not in allowed


def guard_session_stream(response,access,session_token,user_id):
    """已连接事件流同样核对会话；改密／重置后不再向旧连接输出新事件。"""
    original=response.body_iterator
    async def guarded():
        try:
            async for chunk in original:
                actor=access.session(session_token)
                if not actor or actor['id']!=user_id or actor.get('must_change_password'):return
                yield chunk
        finally:
            if hasattr(original,'aclose'):await original.aclose()
    response.body_iterator=guarded()
