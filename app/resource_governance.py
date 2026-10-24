"""SQLite 短事务配额、公平领取与供应商熔断；拒绝和熔断均不重发 HTTP。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from functools import wraps
import sqlite3
from uuid import uuid4

from fastapi import HTTPException

CALL_OWNER = ContextVar('docqa_call_owner', default=None)


def guarded_provider(provider, error_class):
    def decorate(method):
        @wraps(method)
        def invoke(self, *args, **kwargs):
            self._call_metadata.set(None)
            if not getattr(self.settings, provider + '_api_key').strip():
                return method(self, *args, **kwargs)
            with provider_guard(self.settings, provider, error_class):
                return method(self, *args, **kwargs)
        return invoke
    return decorate


def task_owner(method):
    @wraps(method)
    def invoke(self, job, *args, **kwargs):
        owner = job.get('owner_user_id') if isinstance(job, dict) else None
        with owner_scope(owner):
            return method(self, job, *args, **kwargs)
    return invoke


def stamp(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='microseconds')


def policy(repository):
    from app.config import Settings
    return getattr(repository, 'settings', None) or Settings(data_dir=repository.path.parent)


def jobs(db):
    return [dict(r) for r in db.execute("""SELECT 'kb_tasks' AS kind,id,COALESCE(owner_user_id,'local') AS owner,
        status,created_at,lease_expires_at,cancel_requested FROM kb_tasks WHERE status IN ('queued','running')
        UNION ALL SELECT 'question_jobs',id,COALESCE(owner_user_id,'local'),status,created_at,lease_expires_at,cancel_requested
        FROM question_jobs WHERE status IN ('queued','running')
        UNION ALL SELECT 'analysis_jobs',id,'local',status,created_at,lease_expires_at,cancel_requested
        FROM analysis_jobs WHERE status IN ('queued','running')
        UNION ALL SELECT 'chat_submissions',id,COALESCE(owner_user_id,'local'),'queued',created_at,NULL,0
        FROM chat_submissions WHERE status='waiting'""")]


def admit(db, repository, owner, *, submission_id=None):
    settings = policy(repository)
    pending = [r for r in jobs(db) if r['status'] == 'queued' and not r['cancel_requested']
               and not (r['kind'] == 'chat_submissions' and r['id'] == submission_id)]
    if (len(pending) >= settings.queue_global_limit or
            sum(r['owner'] == (owner or 'local') for r in pending) >= settings.queue_user_limit):
        raise HTTPException(429, '等待任务已达配额，请稍后再提交；未发起外部请求', headers={'Retry-After': '30'})


def choose(db, repository, kind, identifier=None):
    """跨问答／分析队列按用户轮换；过期任务交由各原有恢复器处理。"""
    settings = policy(repository)
    rows = jobs(db)
    active = [r for r in rows if r['status'] == 'running' and (r['lease_expires_at'] or '') > stamp()]
    if len(active) >= settings.active_global_limit:
        return None
    counts = {r['owner']: sum(a['owner'] == r['owner'] for a in active) for r in rows}
    last = dict(db.execute('SELECT owner_key,last_started_at FROM dispatch_fairness'))
    eligible = [r for r in rows if r['kind'] == kind and r['status'] == 'queued'
                and not r['cancel_requested'] and counts.get(r['owner'], 0) < settings.active_user_limit]
    eligible.sort(key=lambda r: (last.get(r['owner'], ''), r['created_at'], r['id']))
    if identifier:
        # 显式定向执行仍受同一并发配额约束，不绕过限流。
        eligible = [r for r in eligible if r['id'] == identifier]
    if not eligible:
        return None
    selected = eligible[0]
    db.execute('INSERT INTO dispatch_fairness VALUES (?,?) ON CONFLICT(owner_key) DO UPDATE SET last_started_at=excluded.last_started_at',
               (selected['owner'], stamp()))
    return db.execute(f"SELECT * FROM {kind} WHERE id=?", (selected['id'],)).fetchone()


@contextmanager
def owner_scope(owner):
    token = CALL_OWNER.set(owner or 'local')
    try:
        yield
    finally:
        CALL_OWNER.reset(token)


def _owner():
    if CALL_OWNER.get() is not None:
        return CALL_OWNER.get()
    from app.product_access import actor_context
    actor = actor_context.get()
    return actor['id'] if actor else 'local'


@contextmanager
def provider_guard(settings, provider, error_class):
    """围住适配器的一次 HTTP（流式亦同）。不包含 SDK 自动重试。"""
    path = settings.data_dir / 'docqa.db'
    if not path.exists():
        # 独立适配器可脱离应用使用；应用入口在准备数据库后才可收费。
        yield
        return
    key = sha256((provider + '|' + getattr(settings, provider + '_base_url') + '|' +
                  getattr(settings, provider + '_model')).encode()).hexdigest()
    identifier = 'guard-' + uuid4().hex
    def reject(code,message):
        error=error_class(code,message)
        error.not_sent=True
        raise error
    def connect():
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_circuits'").fetchone():
            reject(503, '运行数据库不支持流量保护，请完成受控升级')
        circuit = db.execute('SELECT * FROM provider_circuits WHERE provider_key=?', (key,)).fetchone()
        now = stamp()
        if circuit and ((circuit['open_until'] or '') > now or (circuit['probe_until'] or '') > now):
            reject(503, '供应商暂时熔断，未发起外部请求；稍后明确重试')
        day = now[:10]
        owner = _owner()
        total, own = db.execute('SELECT COUNT(*),COALESCE(SUM(owner_key=?),0) FROM provider_guard_calls WHERE started_at>=?',
                               (owner, day)).fetchone()
        if total >= settings.calls_global_daily_limit or own >= settings.calls_user_daily_limit:
            reject(429, '本日调用配额已达上限，未发起外部请求')
        db.execute("UPDATE provider_guard_calls SET status='uncertain',finished_at=? WHERE status='intent' AND deadline<=?",(now,now))
        active,own_active=db.execute("SELECT COUNT(*),COALESCE(SUM(owner_key=?),0) FROM provider_guard_calls WHERE status='intent' AND deadline>?",(owner,now)).fetchone()
        if active>=settings.active_global_limit or own_active>=settings.active_user_limit:
            reject(429, '外部请求并发已达配额，未发起外部请求')
        deadline=stamp(getattr(settings,provider+'_timeout_seconds')+60)
        db.execute('INSERT INTO provider_guard_calls VALUES (?,?,?,?,?,NULL,?)', (identifier, owner, key, 'intent', now,deadline))
        if circuit and circuit['failures'] >= settings.circuit_failure_threshold:
            db.execute('UPDATE provider_circuits SET probe_id=?,probe_until=? WHERE provider_key=?',
                       (identifier, stamp(max(settings.deepseek_timeout_seconds, settings.embedding_timeout_seconds) + 60), key))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    failure = None
    try:
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        db = connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                status_code=getattr(failure,'status_code',None) or 500
                upstream_failed = failure is not None and (status_code >= 500 or status_code == 429)
                status = 'uncertain' if failure and (getattr(failure, 'status_code', None) == 504
                    or 'Timeout' in type(failure).__name__ or 'Connection' in type(failure).__name__) else 'failed' if failure else 'succeeded'
                db.execute('UPDATE provider_guard_calls SET status=?,finished_at=? WHERE id=?', (status, stamp(), identifier))
                row = db.execute('SELECT failures FROM provider_circuits WHERE provider_key=?', (key,)).fetchone()
                count = (row[0] if row else 0) + 1 if upstream_failed else 0
                until = stamp(settings.circuit_open_seconds) if count >= settings.circuit_failure_threshold else None
                db.execute('INSERT INTO provider_circuits VALUES (?,?,?,NULL,NULL) ON CONFLICT(provider_key) DO UPDATE SET failures=excluded.failures,open_until=excluded.open_until,probe_id=NULL,probe_until=NULL', (key, count, until))
        finally:
            db.close()


def install_governance_routes(app,access):
    from fastapi import APIRouter
    router=APIRouter()

    @router.get('/api/chat/quota')
    def own_status():
        owner=access.actor['id'] if access.actor else 'local'
        with access.repository.connect() as db:
            rows=[r for r in jobs(db) if r['owner']==owner]
            calls=db.execute('SELECT COUNT(*) FROM provider_guard_calls WHERE owner_key=? AND started_at>=?',(owner,stamp()[:10])).fetchone()[0]
        settings=access.settings
        return {'active':sum(r['status']=='running' for r in rows),'queued':sum(r['status']=='queued' for r in rows),
                'calls_today':calls,'daily_limit':settings.calls_user_daily_limit,
                'active_limit':settings.active_user_limit,'queue_limit':settings.queue_user_limit,'day_boundary':'UTC'}

    @router.get('/api/admin/operations')
    def operations():
        access.admin()
        with access.repository.connect() as db:
            rows=jobs(db)
            circuits=[{'failures':r['failures'],'open_until':r['open_until'],'probe_until':r['probe_until']}
                      for r in db.execute('SELECT * FROM provider_circuits')]
            today=db.execute('SELECT COUNT(*) FROM provider_guard_calls WHERE started_at>=?',(stamp()[:10],)).fetchone()[0]
        from app.runtime import RuntimeGuard
        directory=access.settings.data_dir/'.local-service'
        supervisor={'status':'not_installed_or_stopped'}
        try:
            import json
            record=json.loads((directory/'supervisor.json').read_text(encoding='utf-8-sig'))
            from app.runtime import process_start_token
            if record.get('data_dir')==str(access.settings.data_dir.resolve()):
                supervisor={'status':record.get('status'),'reason':record.get('reason'),
                            'running':bool(record.get('process_start_token') and process_start_token(record.get('pid'))==record['process_start_token'])}
        except (OSError,ValueError,TypeError):pass
        return {'active':sum(r['status']=='running' for r in rows),'queued':sum(r['status']=='queued' for r in rows),
                'calls_today':today,'circuits':circuits,'supervisor':supervisor,
                'services':app.state.runtime_guard.service_status(),'provider_calls_triggered':0}

    app.include_router(router)
