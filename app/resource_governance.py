"""SQLite 短事务配额、公平领取与供应商熔断；拒绝和熔断均不重发 HTTP。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from functools import wraps
from inspect import signature
import sqlite3
from uuid import uuid4

from fastapi import HTTPException

CALL_OWNER = ContextVar('docqa_call_owner', default=None)
PROVIDER_PERMIT = ContextVar('docqa_provider_permit', default=None)


def mark_provider_sent():
    """紧贴HTTP适配器调用；进入SDK后是否到达上游未知，必须保守占用。"""
    permit = PROVIDER_PERMIT.get()
    if permit is not None:
        permit['sent'] = True


def mark_provider_completed():
    permit=PROVIDER_PERMIT.get()
    if permit is not None:permit['completed']=True


def record_provider_usage(metadata):
    permit=PROVIDER_PERMIT.get()
    usage=metadata.get('usage') if isinstance(metadata,dict) else None
    if permit is not None and isinstance(usage,dict):
        permit['usage']={k:v for k,v in usage.items() if k in ('prompt_tokens','completion_tokens','total_tokens','cached_tokens','reasoning_tokens') and type(v) is int and v>=0}


def guarded_provider(provider, error_class):
    def decorate(method):
        parameters = signature(method)
        @wraps(method)
        def invoke(self, *args, **kwargs):
            self._call_metadata.set(None)
            try:
                arguments = parameters.bind(self, *args, **kwargs).arguments
            except TypeError as exc:
                exc.not_sent = True
                raise
            if not getattr(self.settings, provider + '_api_key').strip():
                try:return method(self, *args, **kwargs)
                except Exception as exc:
                    exc.not_sent=True
                    raise
            from app.context_budget import generation_request, request_scope, BudgetError
            try:
                request=generation_request(self.settings,arguments['system_prompt'],arguments['user_prompt'],
                                           stream=method.__name__=='generate_stream')
            except BudgetError as exc:
                error=error_class(429,str(exc));error.not_sent=True
                raise error from None
            with request_scope(request), provider_guard(self.settings, provider, error_class, delivery_tracked=True,budget_request=request):
                try:return method(self, *args, **kwargs)
                finally:record_provider_usage(getattr(self,'last_call_metadata',None))
        return invoke
    return decorate


def task_owner(method):
    @wraps(method)
    def invoke(self, job, *args, **kwargs):
        owner = job.get('owner_user_id') if isinstance(job, dict) else None
        if not isinstance(job,dict) and hasattr(job,'id'):
            with self.repository.connect() as db:
                row=db.execute('SELECT owner_user_id FROM analysis_jobs WHERE id=?',(job.id,)).fetchone()
                owner=row[0] if row else None
        from app.context_budget import task_scope,profile_for
        profile=profile_for(args[0] if args else '',kwargs.get('checkpoint')) if getattr(self,'budget_source',None)=='knowledge' else 'analysis' if not isinstance(job,dict) else 'answer'
        with owner_scope(owner),task_scope(getattr(self,'budget_source','question'),job['id'] if isinstance(job,dict) else job.id,profile):
            return method(self, job, *args, **kwargs)
    return invoke


def stamp(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='microseconds')


def policy(repository):
    from app.config import Settings
    return getattr(repository, 'settings', None) or Settings(data_dir=repository.path.parent)


def jobs(db):
    return [dict(r) for r in db.execute("""SELECT 'kb_tasks' AS kind,id,COALESCE(owner_user_id,'local') AS owner,
        status,created_at,lease_expires_at,cancel_requested,updated_at AS queued_at FROM kb_tasks WHERE status IN ('queued','running')
        UNION ALL SELECT 'question_jobs',id,COALESCE(owner_user_id,'local'),status,created_at,lease_expires_at,cancel_requested,updated_at
        FROM question_jobs WHERE status IN ('queued','running')
        UNION ALL SELECT 'analysis_jobs',id,COALESCE(owner_user_id,'local'),status,created_at,lease_expires_at,cancel_requested,updated_at
        FROM analysis_jobs WHERE status IN ('queued','running')
        UNION ALL SELECT 'chat_submissions',id,COALESCE(owner_user_id,'local'),'queued',created_at,NULL,0,updated_at
        FROM chat_submissions WHERE status='waiting'
        UNION ALL SELECT 'parse_tasks',id,COALESCE(owner_user_id,'local'),status,created_at,lease_expires_at,cancel_requested,updated_at
        FROM parse_tasks WHERE status IN ('queued','running')
        UNION ALL SELECT 'ingestion_jobs',i.id,COALESCE(i.owner_user_id,'local'),'queued',i.created_at,NULL,0,i.updated_at
        FROM ingestion_jobs i WHERE i.status IN ('queued','running')
        AND NOT EXISTS(SELECT 1 FROM parse_tasks p WHERE p.id=i.parse_task_id AND p.status IN ('queued','running'))
        AND NOT EXISTS(SELECT 1 FROM kb_tasks k WHERE k.id=i.index_task_id AND k.status IN ('queued','running'))""")]


def expire_queued(db,settings,rows):
    cutoff=stamp(-settings.queue_wait_seconds)
    expired=[r for r in rows if r['status']=='queued' and (r['queued_at'] or r['created_at'])<cutoff]
    for row in expired:
        table=row['kind'];stamp_now=stamp()
        if table=='chat_submissions':
            db.execute("UPDATE chat_submissions SET status='failed',error_code='queue_timeout',error_message='等待资料或执行超时，请明确重新提交',updated_at=? WHERE id=? AND status='waiting'",(stamp_now,row['id']))
        elif table=='ingestion_jobs':
            db.execute("UPDATE ingestion_jobs SET status='failed',error_code='queue_timeout',error_message='排队等待超时，请明确重试',updated_at=? WHERE id=? AND status IN ('queued','running')",(stamp_now,row['id']))
        else:
            status='needs_attention' if table=='parse_tasks' else 'failed'
            db.execute(f"UPDATE {table} SET status=?,error_code='queue_timeout',error_message='排队等待超时，未发起新的外部请求，请明确重试',updated_at=? WHERE id=? AND status='queued'",(status,stamp_now,row['id']))
    ids={(r['kind'],r['id']) for r in expired}
    return [r for r in rows if (r['kind'],r['id']) not in ids]


def admit(db, repository, owner, *, submission_id=None, ingestion_id=None):
    settings = policy(repository)
    pending = [r for r in expire_queued(db,settings,jobs(db)) if r['status'] == 'queued' and not r['cancel_requested']
               and not (r['kind'] == 'chat_submissions' and r['id'] == submission_id)
               and not (r['kind']=='ingestion_jobs' and r['id']==ingestion_id)]
    if (len(pending) >= settings.queue_global_limit or
            sum(r['owner'] == (owner or 'local') for r in pending) >= settings.queue_user_limit):
        raise HTTPException(429, '等待任务已达配额，请稍后再提交；未发起外部请求', headers={'Retry-After': '30'})


def choose(db, repository, kind, identifier=None, *, ready=None):
    """跨问答／分析队列按用户轮换；过期任务交由各原有恢复器处理。"""
    settings = policy(repository)
    rows = expire_queued(db,settings,jobs(db))
    active = [r for r in rows if r['status'] == 'running' and (r['lease_expires_at'] or '') > stamp()]
    if len(active) >= settings.active_global_limit:
        return None
    counts = {r['owner']: sum(a['owner'] == r['owner'] for a in active) for r in rows}
    last = dict(db.execute('SELECT owner_key,last_started_at FROM dispatch_fairness'))
    eligible = [r for r in rows if r['kind'] == kind and r['status'] == 'queued'
                and not r['cancel_requested'] and counts.get(r['owner'], 0) < settings.active_user_limit]
    if ready:eligible=[r for r in eligible if ready(db,r['id'])]
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
def provider_guard(settings, provider, error_class, *, delivery_tracked=False,budget_request=None,provider_identity=None,timeout_seconds=None):
    """围住适配器的一次 HTTP（流式亦同）。不包含 SDK 自动重试。"""
    path = settings.data_dir / 'docqa.db'
    if not path.exists():
        # 独立适配器可脱离应用使用；应用入口在准备数据库后才可收费。
        yield
        return
    key = sha256((provider_identity or (provider + '|' + getattr(settings, provider + '_base_url') + '|' +
                  getattr(settings, provider + '_model'))).encode()).hexdigest()
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
        if 'meter_kind' not in {r[1] for r in db.execute('PRAGMA table_info(provider_guard_calls)')}:
            reject(503,'运行库缺少统一计量结构，请完成受控升级')
        circuit = db.execute('SELECT * FROM provider_circuits WHERE provider_key=?', (key,)).fetchone()
        now = stamp()
        if circuit and ((circuit['open_until'] or '') > now or (circuit['probe_until'] or '') > now):
            reject(503, '供应商暂时熔断，未发起外部请求；稍后明确重试')
        day = now[:10]
        owner = _owner()
        total, own = db.execute("SELECT COUNT(*),COALESCE(SUM(owner_key=?),0) FROM provider_guard_calls WHERE started_at>=? AND status!='not_sent'",
                               (owner, day)).fetchone()
        if total >= settings.calls_global_daily_limit or own >= settings.calls_user_daily_limit:
            reject(429, '本日调用配额已达上限，未发起外部请求')
        db.execute("UPDATE provider_guard_calls SET status='uncertain',delivery_status='uncertain',finished_at=? WHERE status='intent' AND deadline<=?",(now,now))
        active,own_active=db.execute("SELECT COUNT(*),COALESCE(SUM(owner_key=?),0) FROM provider_guard_calls WHERE status='intent' AND deadline>?",(owner,now)).fetchone()
        if active>=settings.active_global_limit or own_active>=settings.active_user_limit:
            reject(429, '外部请求并发已达配额，未发起外部请求')
        deadline=stamp((timeout_seconds if timeout_seconds is not None else getattr(settings,provider+'_timeout_seconds'))+60)
        db.execute("INSERT INTO provider_guard_calls(id,owner_key,provider_key,status,started_at,deadline,delivery_status,meter_kind) VALUES (?,?,?,?,?,?,?,?)", (identifier, owner, key, 'intent', now,deadline,'reserved','tool_http' if provider=='mcp' else provider))
        if budget_request:
            from app.context_budget import reserve,BudgetError
            try:reserve(db,identifier,settings,owner,budget_request,now)
            except BudgetError as exc:reject(429,str(exc))
            db.execute("UPDATE context_reservations SET state='uncertain' WHERE state='reserved' AND call_id IN (SELECT id FROM provider_guard_calls WHERE status='uncertain')")
        db.execute('INSERT OR IGNORE INTO provider_circuits(provider_key) VALUES (?)', (key,))
        generation = circuit['generation'] if circuit else 0
        if circuit and circuit['failures'] >= settings.circuit_failure_threshold:
            generation += 1
            db.execute('UPDATE provider_circuits SET probe_id=?,probe_until=?,generation=? WHERE provider_key=?',
                       (identifier, stamp(max(settings.deepseek_timeout_seconds, settings.embedding_timeout_seconds) + 60), generation, key))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    failure = None
    permit = {'sent': not delivery_tracked,'completed':False}
    permit_token = PROVIDER_PERMIT.set(permit)
    try:
        yield
    except BaseException as exc:
        failure = exc
        if not permit['sent']:
            exc.not_sent = True
        elif not permit['completed'] and getattr(exc, 'status', None) in ('cancelled', 'needs_attention'):
            # 已发送的流被本地中止，无法证明供应商是否完成；保留费用风险。
            exc.uncertain = True
        raise
    finally:
        PROVIDER_PERMIT.reset(permit_token)
        db = connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                status_code = getattr(failure,'status_code',None)
                local = (getattr(failure, 'status', None) in ('cancelled', 'needs_attention')
                    or bool(getattr(failure, 'not_sent', False)) or bool(getattr(failure,'provider_health_neutral',False)))
                upstream_failed = (failure is not None and not local and permit['sent'] and
                    ((status_code is not None and (status_code >= 500 or status_code == 429))
                     or 'Timeout' in type(failure).__name__ or 'Connection' in type(failure).__name__))
                status = 'uncertain' if failure and (getattr(failure,'uncertain',False) or getattr(failure, 'status_code', None) == 504
                    or 'Timeout' in type(failure).__name__ or 'Connection' in type(failure).__name__) else 'failed' if failure else 'succeeded'
                if not permit['sent']:
                    status = 'not_sent'
                import json
                db.execute('UPDATE provider_guard_calls SET status=?,finished_at=?,delivery_status=?,usage_json=? WHERE id=?',
                    (status, stamp(), 'not_sent' if not permit['sent'] else 'uncertain' if status=='uncertain' else 'sent',json.dumps(permit.get('usage')) if permit.get('usage') else None, identifier))
                if budget_request:
                    from app.context_budget import settle
                    settle(db,identifier,permit,status,stamp())
                row = db.execute('SELECT * FROM provider_circuits WHERE provider_key=?', (key,)).fetchone()
                # 状态代次只在打开／探测／关闭边界变化；同代并发失败仍可累计。
                if row['generation'] == generation and (not row['probe_id'] or row['probe_id'] == identifier):
                    if local:
                        if row['probe_id'] == identifier:
                            db.execute('UPDATE provider_circuits SET probe_id=NULL,probe_until=NULL,generation=generation+1 WHERE provider_key=?', (key,))
                    else:
                        count = row['failures'] + 1 if upstream_failed else 0
                        until = stamp(settings.circuit_open_seconds) if count >= settings.circuit_failure_threshold else None
                        transition = bool(until or row['probe_id'])
                        db.execute('UPDATE provider_circuits SET failures=?,open_until=?,probe_id=NULL,probe_until=NULL,generation=generation+? WHERE provider_key=?',
                            (count, until, int(transition), key))
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
            calls=db.execute("SELECT COUNT(*) FROM provider_guard_calls WHERE owner_key=? AND started_at>=? AND status!='not_sent'",(owner,stamp()[:10])).fetchone()[0]
        settings=access.settings
        from app.context_budget import status as budget_status
        with access.repository.connect() as db:context_budget=budget_status(db,settings,owner=owner)
        return {'active':sum(r['status']=='running' for r in rows),'queued':sum(r['status']=='queued' for r in rows),
                'context_budget':context_budget,'provider_calls_triggered':0,
                'calls_today':calls,'daily_limit':settings.calls_user_daily_limit,
                'active_limit':settings.active_user_limit,'queue_limit':settings.queue_user_limit,'day_boundary':'UTC'}

    @router.get('/api/admin/operations')
    def operations():
        access.admin()
        with access.repository.connect() as db:
            rows=jobs(db)
            circuits=[{'failures':r['failures'],'open_until':r['open_until'],'probe_until':r['probe_until']}
                      for r in db.execute('SELECT * FROM provider_circuits')]
            today=db.execute("SELECT COUNT(*) FROM provider_guard_calls WHERE started_at>=? AND status!='not_sent'",(stamp()[:10],)).fetchone()[0]
            usage_rows=db.execute("SELECT usage_json,status,deadline FROM provider_guard_calls WHERE started_at>=? AND status!='not_sent' AND COALESCE(meter_kind,'legacy')!='tool_http'",(stamp()[:10],)).fetchall()
            stored=db.execute('SELECT COALESCE(SUM(byte_size),0) FROM document_storage_owners').fetchone()[0]
            reserved=db.execute('SELECT COALESCE(SUM(byte_size),0) FROM storage_reservations').fetchone()[0]
            from app.operations_metrics import project
            latency=project(db,stamp()[:10])
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
        import json,shutil
        known_tokens=sum((json.loads(r['usage_json']) if r['usage_json'] else {}).get('total_tokens',0) for r in usage_rows)
        unknown=sum(not r['usage_json'] or 'total_tokens' not in json.loads(r['usage_json']) for r in usage_rows)
        uncertain=sum(r['status']=='uncertain' or r['status']=='intent' and r['deadline']<stamp() for r in usage_rows)
        services=app.state.runtime_guard.service_status()
        from app.operations_metrics import backup_status
        last_backup=backup_status(access.settings.data_dir)
        from app.resident_audit import summarize
        session_audit=summarize(access.settings.data_dir,fingerprint=getattr(app.state.runtime_guard,'fingerprint',None))
        session_evidence={k:session_audit[k] for k in ('startup_count','invalid_records','incomplete_tails','different_logon_context_observed','real_logoff_logon_verified')}
        alerts=[]
        if uncertain:alerts.append({'code':'uncertain_calls','severity':'attention','count':uncertain})
        if reserved:alerts.append({'code':'pending_storage_reservations','severity':'attention','count':reserved})
        if any(not w['ready'] for w in services['workers']):alerts.append({'code':'worker_not_ready','severity':'error','count':sum(not w['ready'] for w in services['workers'])})
        if any((c['open_until'] or '')>stamp() or (c['probe_until'] or '')>stamp() for c in circuits):alerts.append({'code':'provider_circuit','severity':'attention','count':1})
        if last_backup['status']=='unknown' or last_backup.get('age_seconds',0)>7*86400:alerts.append({'code':'backup_unverified_or_old','severity':'attention','count':1})
        if session_audit['invalid_records']:alerts.append({'code':'resident_audit_invalid','severity':'attention','count':session_audit['invalid_records']})
        disk=shutil.disk_usage(access.settings.data_dir)
        if disk.free<access.settings.storage_free_min_mb*1024*1024:alerts.append({'code':'disk_low','severity':'error','count':disk.free})
        from app.context_budget import status as budget_status
        with access.repository.connect() as db:context_budget=budget_status(db,access.settings)
        return {'active':sum(r['status']=='running' for r in rows),'queued':sum(r['status']=='queued' for r in rows),
                'context_budget':context_budget,
                'calls_today':today,'circuits':circuits,'supervisor':supervisor,
                'services':services,'alerts':alerts,'latency':latency,'last_backup':last_backup,'resident_session_evidence':session_evidence,'alert_delivery':'in_app_only',
                'usage':{'known_total_tokens':known_tokens,'unknown_usage_calls':unknown,'verified_cost':None,'currency':None},
                'storage':{'original_bytes':stored,'reserved_bytes':reserved,'disk_free_bytes':disk.free,'global_limit_bytes':access.settings.storage_global_mb*1024*1024},
                'limits':{'active_global':access.settings.active_global_limit,'active_user':access.settings.active_user_limit,
                    'queue_global':access.settings.queue_global_limit,'queue_user':access.settings.queue_user_limit,'queue_wait_seconds':access.settings.queue_wait_seconds},
                'provider_calls_triggered':0,'day_boundary':'UTC'}

    @router.get('/admin/operations')
    def operations_page():
        access.admin()
        from pathlib import Path
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).parent/'web'/'operations-admin.html')

    app.include_router(router)
