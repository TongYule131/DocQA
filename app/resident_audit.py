"""驻留跨会话证据：仅记录本进程与Windows登录标识，不采集用户名或业务内容。"""
import argparse,json,logging,os,re
from datetime import datetime,timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

VERSION='resident-lifecycle-audit-v1'
STATUSES={'ready','recovering','stopped','needs_attention'}
REASONS={'code_or_schema_changed','restart_limit','restart_history_invalid','stop_timeout_or_launch_failed','stop_failed','start_timeout_or_launch_failed','start_failed'}


class _AuditHandler(RotatingFileHandler):
    def handleError(self,record):raise OSError('resident_audit_write_failed')


def current_logon_context():
    if os.name!='nt':return None
    import ctypes
    from ctypes import wintypes
    class Luid(ctypes.Structure):_fields_=[('low',ctypes.c_uint32),('high',ctypes.c_int32)]
    class Statistics(ctypes.Structure):
        _fields_=[('token_id',Luid),('authentication_id',Luid),('expires',ctypes.c_int64),
            ('token_type',wintypes.DWORD),('impersonation',wintypes.DWORD),('charged',wintypes.DWORD),
            ('available',wintypes.DWORD),('groups',wintypes.DWORD),('privileges',wintypes.DWORD),('modified',Luid)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);security=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.ProcessIdToSessionId.argtypes=[wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)];kernel.ProcessIdToSessionId.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
    security.OpenProcessToken.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)];security.OpenProcessToken.restype=wintypes.BOOL
    security.GetTokenInformation.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)];security.GetTokenInformation.restype=wintypes.BOOL
    token=wintypes.HANDLE();session=wintypes.DWORD();size=wintypes.DWORD();stats=Statistics()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(token)):return None
    try:
        if not security.GetTokenInformation(token,10,ctypes.byref(stats),ctypes.sizeof(stats),ctypes.byref(size)):return None
        if not kernel.ProcessIdToSessionId(os.getpid(),ctypes.byref(session)):return None
        luid=stats.authentication_id
        return {'session_id':session.value,'logon_id':f'{luid.high & 0xffffffff:08x}:{luid.low:08x}'}
    finally:kernel.CloseHandle(token)


def record(directory,event,state):
    if event not in {'supervisor_started','state_changed'}:raise ValueError('invalid_resident_audit_event')
    if state.get('status') not in STATUSES|{None} or state.get('reason') not in REASONS|{None}:return False
    value={'version':VERSION,'event':event,'checked_at':datetime.now(timezone.utc).isoformat()}
    for key in ('pid','process_start_token','code_fingerprint','status','reason'):
        item=state.get(key)
        if item is not None:
            if key=='pid' and type(item) is not int:return False
            if key!='pid' and (not isinstance(item,str) or len(item)>128):return False
            value[key]=item
    context=state.get('logon_context')
    if context is not None:
        if (not isinstance(context,dict) or set(context)!={'session_id','logon_id'} or type(context['session_id']) is not int
            or not isinstance(context['logon_id'],str) or not re.fullmatch(r'[0-9a-f]{8}:[0-9a-f]{8}',context['logon_id'])):return False
        value['logon_context']=context
    directory=Path(directory);path=directory/'resident-audit.jsonl'
    handler=None
    try:
        directory.mkdir(parents=True,exist_ok=True)
        if any(p.is_symlink() for p in [path,*[Path(str(path)+'.'+str(i)) for i in range(1,4)]]):return False
        handler=_AuditHandler(path,maxBytes=1024*1024,backupCount=3,encoding='utf-8')
        # 不把调用方整份state、路径、异常正文或任何客户资料写入日志。
        record=logging.LogRecord('docqa.resident.audit',logging.INFO,'',0,json.dumps(value,ensure_ascii=False),(),None)
        handler.emit(record);handler.flush();os.fsync(handler.stream.fileno());return True
    except OSError:return False
    finally:
        if handler is not None:handler.close()


def summarize(data_dir,*,fingerprint=None):
    directory=Path(data_dir)/'.local-service';base=directory/'resident-audit.jsonl';events=[];invalid=0;incomplete=0
    for path in [*[Path(str(base)+'.'+str(i)) for i in (3,2,1)],base]:
        if not path.exists():continue
        try:
            if path.is_symlink() or path.stat().st_size>2*1024*1024:invalid+=1;continue
            text=path.read_text('utf-8');lines=text.splitlines()
            if text and not text.endswith('\n'):lines=lines[:-1];incomplete+=1
        except (OSError,UnicodeError):invalid+=1;continue
        for line in lines:
            try:
                value=json.loads(line)
                allowed={'version','event','checked_at','pid','process_start_token','code_fingerprint','status','reason','logon_context'}
                if not isinstance(value,dict) or value.get('version')!=VERSION or set(value)-allowed or value.get('event') not in {'supervisor_started','state_changed'}:raise ValueError()
                if not datetime.fromisoformat(value['checked_at']).tzinfo:raise ValueError()
                if value.get('status') not in STATUSES|{None} or value.get('reason') not in REASONS|{None}:raise ValueError()
                if 'pid' in value and type(value['pid']) is not int:raise ValueError()
                for key in ('process_start_token','code_fingerprint'):
                    if key in value and (not isinstance(value[key],str) or len(value[key])>128):raise ValueError()
                context=value.get('logon_context')
                if context is not None and (not isinstance(context,dict) or set(context)!={'session_id','logon_id'}
                    or type(context['session_id']) is not int or not isinstance(context['logon_id'],str)
                    or not re.fullmatch(r'[0-9a-f]{8}:[0-9a-f]{8}',context['logon_id'])):raise ValueError()
                events.append(value)
            except (ValueError,TypeError,KeyError):invalid+=1
    startups=[e for e in events if e.get('event')=='supervisor_started' and (fingerprint is None or e.get('code_fingerprint')==fingerprint)]
    groups={}
    for event in startups:
        context=event.get('logon_context') or {}
        if isinstance(context.get('logon_id'),str):groups.setdefault(event.get('code_fingerprint'),set()).add(context['logon_id'])
    return {'version':VERSION,'events':events,'startup_count':len(startups),'invalid_records':invalid,'incomplete_tails':incomplete,
        'different_logon_context_observed':any(len(v)>1 for v in groups.values()),
        'real_logoff_logon_verified':False,'scope':'进程启动与登录标识证据；真实注销／重新登录还需用户操作确认',
        'retention':'1MB × 最多4份；轮转仅影响驻留审计，不删除调用账本或业务数据','provider_calls':0}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data-dir',type=Path,required=True);parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv);report=summarize(args.data_dir)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k!='events'},ensure_ascii=False))


if __name__=='__main__':main()
