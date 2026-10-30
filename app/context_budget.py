"""实际请求准入与持久化计量。估算不是供应商token硬保证，未知费用不记零。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json

VERSION = 'context-budget-v1'
TASK = ContextVar('docqa_budget_task', default=None)
REQUEST = ContextVar('docqa_budget_request', default=None)
PROFILES = {'default','general_dispatch','planning','answer','review','tool_parameters','analysis','analysis_reduce','embedding'}
# 仅可信服务端代码可登记计数器，不能通过前端或模型返回安装／指定执行代码。
COUNTERS = {}


class BudgetError(ValueError):
    def __init__(self,message,*,code='budget'):
        super().__init__(message);self.code=code


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))


def fingerprint(value):
    return hashlib.sha256(dump(value).encode('utf-8')).hexdigest()


def binding(settings, provider):
    return fingerprint([provider,getattr(settings,provider+'_base_url').rstrip('/'),getattr(settings,provider+'_model')])


def policy(settings):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate_budget_key')
            result[key]=value
        return result
    try:
        value=json.loads(settings.context_budget_policy,object_pairs_hook=pairs)
        if not isinstance(value,dict) or set(value)-{'task_limit','user_daily_limit','global_daily_limit','product_window','safety_tokens','outputs','capabilities'}:
            raise ValueError('budget_fields')
        result={'task_limit':2000000,'user_daily_limit':100000000,'global_daily_limit':1000000000,
                'product_window':0,'safety_tokens':2048,'outputs':{},'capabilities':{},**value}
        for name in ('task_limit','user_daily_limit','global_daily_limit','product_window','safety_tokens'):
            if type(result[name]) is not int or not (0 if name in ('product_window','safety_tokens') else 1)<=result[name]<=10**12:
                raise ValueError('budget_limit')
        if result['user_daily_limit']>result['global_daily_limit']:raise ValueError('budget_daily_order')
        if not isinstance(result['outputs'],dict) or set(result['outputs'])-PROFILES:raise ValueError('budget_profiles')
        if any(type(v) is not int or not 1<=v<=settings.deepseek_max_tokens for v in result['outputs'].values()):
            raise ValueError('budget_output')
        if not isinstance(result['capabilities'],dict) or set(result['capabilities'])-{'deepseek','embedding'}:raise ValueError('budget_providers')
        for provider,cap in result['capabilities'].items():
            if not isinstance(cap,dict) or set(cap)-{'binding','window','max_output','counter_id','counter_version','verification'}:
                raise ValueError('budget_capability')
            if not isinstance(cap.get('binding'),str) or len(cap['binding'])!=64:raise ValueError('budget_binding')
            for name in ('window','max_output'):
                if name in cap and (type(cap[name]) is not int or not 1<=cap[name]<=10**9):raise ValueError('budget_window')
            for name in ('counter_id','counter_version','verification'):
                if name in cap and (not isinstance(cap[name],str) or not 1<=len(cap[name])<=200):raise ValueError('budget_counter')
        return result
    except (ValueError,TypeError):
        raise ValueError('DOCQA_CONTEXT_BUDGET_POLICY格式或额度无效') from None


def policy_hash(settings):
    return fingerprint([VERSION,policy(settings),settings.deepseek_max_tokens,binding(settings,'deepseek'),
                        binding(settings,'embedding'),settings.deepseek_thinking,settings.deepseek_reasoning_effort,
                        settings.question_input_max_chars,settings.analysis_input_max_chars,settings.rag_input_max_chars])


@contextmanager
def task_scope(source, identifier, profile='default'):
    token=TASK.set({'key':source+':'+identifier,'profile':profile})
    try:yield
    finally:TASK.reset(token)


def set_profile(profile):
    if profile not in PROFILES:raise ValueError('invalid_budget_profile')
    task=TASK.get()
    if task is not None:task['profile']=profile


@contextmanager
def profile_scope(profile):
    old=TASK.get();token=TASK.set({**(old or {'key':None}),'profile':profile})
    try:yield
    finally:TASK.reset(token)


def profile_for(role,checkpoint=None):
    if role=='tool_parameters':return 'tool_parameters'
    if checkpoint=='auto_decision':return 'general_dispatch'
    if checkpoint=='semantic_review':return 'review'
    if checkpoint in ('composition_plan','query_understanding') or role=='query_planning':return 'planning'
    return 'answer'


@contextmanager
def request_scope(request):
    token=REQUEST.set(request)
    try:yield
    finally:REQUEST.reset(token)


def effective_output(settings):
    return REQUEST.get()['output_reserved'] if REQUEST.get() else settings.deepseek_max_tokens


def prepare(settings, provider, payload, *, profile=None):
    rules=policy(settings);profile=profile or (TASK.get() or {}).get('profile','default')
    if provider=='embedding':profile='embedding'
    if profile not in PROFILES:raise BudgetError('调用阶段无效，未发起请求')
    cap=rules['capabilities'].get(provider,{})
    if cap and cap['binding']!=binding(settings,provider):
        raise BudgetError('模型或网关与能力登记不匹配，未发起请求')
    output=0 if provider=='embedding' else rules['outputs'].get(profile,settings.deepseek_max_tokens)
    if provider=='deepseek' and 'max_output' in cap:output=min(output,cap['max_output'])
    # 与SDK实参一致：extra_body会合并到HTTP JSON顶层，不计一个虚构的消息层。
    actual={**payload,**payload.get('extra_body',{})};actual.pop('extra_body',None)
    if provider=='deepseek':actual['max_tokens']=output
    texts=[m['content'] for m in actual['messages']] if provider=='deepseek' else actual['input']
    if any(not isinstance(t,str) for t in texts):raise BudgetError('只接受文本输入，未发起请求')
    chars=sum(map(len,texts))
    char_limit=max(settings.question_input_max_chars,settings.analysis_input_max_chars,settings.rag_input_max_chars)
    if chars>char_limit:raise BudgetError('完整消息超过字符硬限，请缩小本轮范围；未发起请求',code='input_limit')
    serialized=dump(actual);counter_id=cap.get('counter_id');counter_version=cap.get('counter_version')
    counter=COUNTERS.get((counter_id,counter_version,binding(settings,provider)))
    exact=bool(counter and cap.get('verification'))
    if counter_id and not exact:
        raise BudgetError('登记计数器或校验版本不可用，未扩大上下文；未发起请求')
    units=counter(actual) if exact else len(serialized.encode('utf-8'))+256
    if type(units) is not int or units<1:raise BudgetError('输入计数无效，未发起请求')
    windows=[v for v in (cap.get('window'),rules['product_window']) if v]
    window=min(windows) if windows else None
    available=window-output-rules['safety_tokens'] if window else None
    if available is not None and units>available:
        raise BudgetError('输入与输出预留超过已登记上下文额度，请缩小本轮范围；未发起请求',code='input_limit')
    return {'provider':provider,'profile':profile,'input_units':units,'output_reserved':output,'reserved_units':units+output,
            'request_hash':fingerprint(actual),'policy_hash':policy_hash(settings),'rules':rules,
            'metadata':{'version':VERSION,'model':getattr(settings,provider+'_model'),'binding':binding(settings,provider),
                'profile':profile,'counting_status':'verified' if exact else 'estimated','counter_id':counter_id or 'utf8-serialized-estimate',
                'counter_version':counter_version or '1','input_chars':chars,'input_units':units,'output_reserved':output,
                'context_window':window,'input_available':available,'safety_tokens':rules['safety_tokens'],
                'occupancy_percent':round(units*100/available,1) if exact and available and available>0 else None,
                'context_guarantee':'verified_counter' if exact and window else 'unknown','money_cost':None}}


def generation_request(settings,system,user,*,stream=False):
    payload={'model':settings.deepseek_model,'messages':[{'role':'system','content':system},{'role':'user','content':user}],
             'stream':stream,'max_tokens':settings.deepseek_max_tokens,'reasoning_effort':settings.deepseek_reasoning_effort,
             'extra_body':{'thinking':{'type':settings.deepseek_thinking}}}
    if stream:payload['stream_options']={'include_usage':True}
    return prepare(settings,'deepseek',payload)


def fit_general_history(settings,system,make_user,history):
    """只移除完整的早期轮次，至少保留最近两轮；不裁剪原问、规则、笔记或证据。"""
    kept=list(history);removed=[]
    with profile_scope('general_dispatch'):
        while True:
            user=make_user(kept)
            try:request=generation_request(settings,system,user,stream=True)
            except BudgetError as exc:
                if exc.code!='input_limit' or len(kept)<=2:raise
                item=kept.pop(0);removed.append(item.get('source_turn'))
            else:
                return user,{'removed_source_turns':removed,'retained_turns':len(kept),
                    'original_turns':len(history),'input_chars':request['metadata']['input_chars'],
                    'strategy':'whole_early_turns_only','model_summary_calls':0}


def reserve(db, identifier, settings, owner, request, now):
    if not request:return
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='context_reservations'").fetchone():
        raise BudgetError('运行库缺少上下文计量结构，请完成受控升级；未发起请求')
    rules=request['rules'];task_key=(TASK.get() or {}).get('key') or 'standalone:'+identifier
    metadata=request['metadata']
    if metadata['counting_status']=='verified' and db.execute("""SELECT 1 FROM context_reservations
        WHERE state='overrun' AND json_extract(metadata_json,'$.counting_status')='verified'
        AND json_extract(metadata_json,'$.binding')=? AND json_extract(metadata_json,'$.counter_id')=?
        AND json_extract(metadata_json,'$.counter_version')=? LIMIT 1""",
        (metadata['binding'],metadata['counter_id'],metadata['counter_version'])).fetchone():
        raise BudgetError('当前计数版本已有实报偏差，须核对并登记新版本；未继续请求')
    task=db.execute('SELECT * FROM context_budget_tasks WHERE task_key=?',(task_key,)).fetchone()
    if task and (task['owner_key']!=owner or task['policy_hash']!=request['policy_hash']):
        raise BudgetError('任务额度配置或归属已变化，请新建任务；未发起请求')
    if not task:
        db.execute('INSERT INTO context_budget_tasks VALUES (?,?,?,?,?)',(task_key,owner,request['policy_hash'],rules['task_limit'],now))
    used=db.execute('SELECT COALESCE(SUM(charged_units),0) FROM context_reservations WHERE task_key=?',(task_key,)).fetchone()[0]
    if db.execute("SELECT 1 FROM context_reservations WHERE task_key=? AND state='overrun'",(task_key,)).fetchone():
        raise BudgetError('本任务实报超出预留，需核对计数器和额度；未继续请求')
    day=now[:10]
    total,own=db.execute('SELECT COALESCE(SUM(charged_units),0),COALESCE(SUM(CASE WHEN owner_key=? THEN charged_units ELSE 0 END),0) FROM context_reservations WHERE day=?',(owner,day)).fetchone()
    legacy=legacy_usage(db,settings,day,exclude=identifier)
    total+=sum(v for _,v,_ in legacy);own+=sum(v for who,v,_ in legacy if who==owner)
    amount=request['reserved_units']
    if used+amount>rules['task_limit'] or own+amount>rules['user_daily_limit'] or total+amount>rules['global_daily_limit']:
        raise BudgetError('任务或UTC本日上下文计量额度不足，未发起请求；未知用量仍保留预留')
    db.execute('''INSERT INTO context_reservations(call_id,task_key,owner_key,day,provider,profile,input_units,
        output_reserved,reserved_units,charged_units,state,request_hash,metadata_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,'reserved',?,?,?)''',
        (identifier,task_key,owner,day,request['provider'],request['profile'],request['input_units'],request['output_reserved'],amount,amount,request['request_hash'],dump(request['metadata']),now))


def settle(db, identifier, permit, status, now):
    row=db.execute('SELECT * FROM context_reservations WHERE call_id=?',(identifier,)).fetchone()
    if not row:return
    usage=permit.get('usage') or {};total=usage.get('total_tokens');prompt=usage.get('prompt_tokens');completion=usage.get('completion_tokens')
    valid=type(total) is int and total>0
    if row['provider']=='deepseek':valid=valid and type(prompt) is int and type(completion) is int and prompt+completion==total
    elif type(prompt) is int:valid=valid and prompt==total
    state='unknown';charge=row['reserved_units']
    if not permit['sent']:state,charge='not_sent',0
    elif status=='uncertain':state,charge='uncertain',max(charge,total if valid else 0)
    elif valid:
        metadata=json.loads(row['metadata_json'])
        mismatch=metadata['counting_status']=='verified' and type(prompt) is int and prompt>row['input_units']
        charge=total;state='overrun' if total>row['reserved_units'] or mismatch else 'reported'
    db.execute('UPDATE context_reservations SET state=?,charged_units=?,usage_json=?,settled_at=? WHERE call_id=?',
               (state,charge,dump(usage) if usage else None,now,identifier))


def status(db, settings, *, owner=None, task_key=None):
    rules=policy(settings);day=datetime.now(timezone.utc).isoformat()[:10]
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='context_reservations'").fetchone():
        return {'version':VERSION,'available':False,'reported_tokens':None,'held_units':None,'charged_units':None,
                'remaining_units':None,'unknown_calls':None,'overrun_calls':None,'last_request':None,
                'unit_note':'历史结构尚无统一计量登记；未知不是零','verified_cost':None,'day_boundary':'UTC'}
    where=['day=?'];args=[day]
    if owner is not None:where.append('owner_key=?');args.append(owner)
    if task_key is not None:where=['task_key=?'];args=[task_key]
    rows=db.execute('SELECT state,charged_units,reserved_units,metadata_json FROM context_reservations WHERE '+' AND '.join(where)+' ORDER BY created_at,rowid',args).fetchall()
    known=sum(r['charged_units'] for r in rows if r['state'] in ('reported','overrun'))
    held=sum(r['charged_units'] for r in rows if r['state'] not in ('reported','overrun','not_sent'))
    legacy=[] if task_key else [r for r in legacy_usage(db,settings,day) if owner is None or r[0]==owner]
    task=db.execute('SELECT task_limit,policy_hash FROM context_budget_tasks WHERE task_key=?',(task_key,)).fetchone() if task_key else None
    known+=sum(v for _,v,valid in legacy if valid);held+=sum(v for _,v,valid in legacy if not valid)
    limit=task['task_limit'] if task else rules['task_limit'] if task_key else rules['user_daily_limit'] if owner is not None else rules['global_daily_limit']
    return {'version':VERSION,'available':True,'reported_tokens':known,'held_units':held,'charged_units':known+held,
            'remaining_units':max(0,limit-known-held),'limit_kind':'task' if task_key else 'UTC_daily',
            'unknown_calls':sum(r['state'] in ('reserved','unknown','uncertain') for r in rows)+sum(not valid for _,_,valid in legacy),
            'legacy_calls':len(legacy),
            'overrun_calls':sum(r['state']=='overrun' for r in rows),'daily_limit':rules['user_daily_limit'] if owner is not None else rules['global_daily_limit'],
            'task_limit':task['task_limit'] if task else rules['task_limit'],'policy_changed':bool(task and task['policy_hash']!=policy_hash(settings)),
            'day_boundary':'UTC','unit_note':'预留含估算；未知用量不记零，不能据此计算真实费用',
            'last_request':json.loads(rows[-1]['metadata_json']) if rows else None,'verified_cost':None}


def legacy_usage(db,settings,day,*,exclude=''):
    # 升级当日旧请求不能凭新增表为空就重新获得全部日额度；不回写旧账本。
    rows=db.execute("SELECT p.owner_key,p.usage_json FROM provider_guard_calls p LEFT JOIN context_reservations c ON c.call_id=p.id WHERE p.started_at>=? AND p.started_at<? AND p.status!='not_sent' AND COALESCE(p.meter_kind,'legacy')!='tool_http' AND c.call_id IS NULL AND p.id!=?",(day,day+'T~',exclude)).fetchall()
    reserve_units=max(settings.question_input_max_chars,settings.analysis_input_max_chars,settings.rag_input_max_chars)*4+settings.deepseek_max_tokens+512
    result=[]
    for row in rows:
        try:usage=json.loads(row['usage_json']) if row['usage_json'] else {}
        except (ValueError,TypeError):usage={}
        if not isinstance(usage,dict):usage={}
        total=usage.get('total_tokens')
        valid=type(total) is int and total>0
        if valid and all(type((usage or {}).get(k)) is int for k in ('prompt_tokens','completion_tokens')):
            valid=usage['prompt_tokens']+usage['completion_tokens']==total
        result.append((row['owner_key'],total if valid else reserve_units,valid))
    return result
