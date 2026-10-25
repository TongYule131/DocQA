"""Windows 驻留监管。复用身份核对后的启停脚本，代码／库不匹配即停止接管。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from app.runtime import RuntimeGuard, PROJECT_ROOT, process_start_token


def write_state(directory, value):
    path = directory / 'supervisor.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


@contextmanager
def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0');handle.flush();handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def roles_ready(directory, guard):
    for role in ('web', 'parse-worker', 'analysis-worker'):
        path = directory / (role + '.ready')
        try:
            if path.stat().st_size > 65536:
                return False
            record = json.loads(path.read_text(encoding='utf-8-sig'))
            if (record.get('data_dir') != str(guard.data_dir) or record.get('code_fingerprint') != guard.fingerprint
                    or record.get('schema_version') != __import__('app.migrations', fromlist=['SCHEMA_VERSION']).SCHEMA_VERSION
                    or not record.get('process_start_token')
                    or process_start_token(record.get('pid')) != record['process_start_token']):
                return False
            heartbeat=json.loads((directory/(role+'.heartbeat')).read_text(encoding='utf-8'))
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(heartbeat['checked_at'])).total_seconds()
            if (heartbeat.get('pid')!=record['pid'] or heartbeat.get('role')!=role or heartbeat.get('data_dir')!=str(guard.data_dir)
                or heartbeat.get('process_start_token')!=record['process_start_token'] or heartbeat.get('code_fingerprint')!=guard.fingerprint
                or not -30<=age<=heartbeat.get('grace_seconds',30)):return False
        except (OSError, ValueError, TypeError,KeyError):
            return False
    return True


def rotate_logs(directory, max_bytes=10 * 1024 * 1024):
    # 仅在角色均已停止、文件句柄释放后轮换；最多保留上一份，不删业务文件。
    for path in directory.glob('*.log'):
        if path.is_symlink():
            continue
        if path.stat().st_size > max_bytes:
            path.replace(path.with_suffix(path.suffix + '.previous'))


def script(name, data_dir, port=None):
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-File', str(PROJECT_ROOT / 'scripts' / name), '-DataDir', str(data_dir)]
    if name == 'stop_local.ps1':
        command += ['-KeepSupervisor']
    if port is not None:
        command += ['-Port', str(port)]
    # Windows 后台角色可能继承管道句柄；communicate()会等到角色退出才收到EOF。
    # 直接写文件并等待脚本进程，不能把长期存活角色当作启动脚本仍在运行。
    directory=Path(data_dir)/'.local-service';directory.mkdir(parents=True,exist_ok=True)
    output=directory/('supervisor-'+Path(name).stem+'.log')
    with output.open('w+b') as stream:
        result=subprocess.run(command,cwd=PROJECT_ROOT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
            stdout=stream,stderr=subprocess.STDOUT,timeout=180,check=False)
        stream.seek(0)
        return subprocess.CompletedProcess(result.args,result.returncode,stdout=stream.read(65536))


def supervise(data_dir, port=8000, *, max_cycles=None, sleep=time.sleep, run_script=script, resume=False):
    guard = RuntimeGuard(data_dir, 'supervisor')
    directory = guard.data_dir / '.local-service'
    with exclusive(directory / 'supervisor.lock'):
        if resume:(directory/'supervisor.stop').unlink(missing_ok=True)
        window = directory / 'restart-window.json'
        try:
            attempts = json.loads(window.read_text(encoding='utf-8')) if window.exists() else []
            import math
            if not isinstance(attempts,list) or any(type(t) not in (int,float) or not math.isfinite(t) or t<=0 for t in attempts):
                raise ValueError('invalid restart history')
        except (OSError,ValueError,TypeError):
            write_state(directory,{'status':'needs_attention','reason':'restart_history_invalid'})
            return 3
        base = {'pid': os.getpid(), 'process_start_token': process_start_token(os.getpid()),
                'data_dir': str(guard.data_dir), 'code_fingerprint': guard.fingerprint, 'port': port}
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            if (directory / 'supervisor.stop').exists():
                write_state(directory, {**base, 'status': 'stopped'})
                return 0
            try:
                guard.check()
            except Exception:
                write_state(directory, {**base, 'status': 'needs_attention', 'reason': 'code_or_schema_changed'})
                return 2
            if roles_ready(directory, guard):
                write_state(directory, {**base, 'status': 'ready', 'checked_at': datetime.now(timezone.utc).isoformat()})
                sleep(5)
                continue
            now = time.time()
            attempts = [t for t in attempts if now - t < 600]
            if len(attempts) >= 3:
                write_state(directory, {**base, 'status': 'needs_attention', 'reason': 'restart_limit'})
                return 3
            write_state(directory, {**base, 'status': 'recovering', 'restart_count': len(attempts) + 1})
            attempts.append(now)
            temporary = window.with_suffix('.tmp')
            temporary.write_text(json.dumps(attempts),encoding='utf-8')
            temporary.replace(window)
            # stop_local 会写停止标志；监管内部恢复通过专用参数保留监管器。
            try:
                result = run_script('stop_local.ps1', guard.data_dir)
            except (OSError,subprocess.TimeoutExpired):
                write_state(directory,{**base,'status':'needs_attention','reason':'stop_timeout_or_launch_failed'})
                return 4
            if result.returncode != 0:
                write_state(directory, {**base, 'status': 'needs_attention', 'reason': 'stop_failed'})
                return 4
            if (directory / 'supervisor.stop').exists():
                write_state(directory, {**base, 'status': 'stopped'})
                return 0
            rotate_logs(directory)
            try:
                result = run_script('start_local.ps1', guard.data_dir, port)
            except (OSError,subprocess.TimeoutExpired):
                write_state(directory,{**base,'status':'needs_attention','reason':'start_timeout_or_launch_failed'})
                return 5
            with (directory / 'supervisor.log').open('ab') as log:
                log.write(result.stdout or b'')
            if result.returncode != 0:
                write_state(directory, {**base, 'status': 'needs_attention', 'reason': 'start_failed'})
                return 5
            sleep(min(5 * 2 ** (len(attempts) - 1), 30))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1..65535')
    if os.name != 'nt':
        parser.error('Windows resident supervisor only')
    return supervise(args.data_dir, args.port,resume=args.resume)


if __name__ == '__main__':
    sys.exit(main())
