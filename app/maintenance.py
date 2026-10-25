"""角色持有共享生命周期锁；备份／恢复／清理独占同一锁。"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path


@contextmanager
def file_lock(path,*,shared=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    try:
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
                    ('Offset',wintypes.DWORD),('OffsetHigh',wintypes.DWORD),('hEvent',wintypes.HANDLE)]
            api=ctypes.WinDLL('kernel32',use_last_error=True)
            api.LockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
            api.LockFileEx.restype=wintypes.BOOL
            api.UnlockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
            api.UnlockFileEx.restype=wintypes.BOOL
            native=msvcrt.get_osfhandle(handle.fileno());overlapped=Overlapped()
            if not api.LockFileEx(native,1|(0 if shared else 2),0,1,0,ctypes.byref(overlapped)):
                raise RuntimeError('数据目录正在运行或维护，未取得互斥许可')
            try:yield
            finally:api.UnlockFileEx(native,0,1,0,ctypes.byref(overlapped))
        else:
            import fcntl
            try:fcntl.flock(handle.fileno(),(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('数据目录正在运行或维护，未取得互斥许可') from None
            try:yield
            finally:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
    finally:handle.close()


@contextmanager
def start_mutex(data_dir):
    if os.name!='nt':
        yield
        return
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];api.CreateMutexW.restype=wintypes.HANDLE
    api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];api.WaitForSingleObject.restype=wintypes.DWORD
    api.ReleaseMutex.argtypes=[wintypes.HANDLE];api.CloseHandle.argtypes=[wintypes.HANDLE]
    name='Local\\DocQA-start-'+sha256(str(Path(data_dir).resolve()).lower().encode()).hexdigest().upper()
    handle=api.CreateMutexW(None,False,name)
    if not handle:raise RuntimeError('不能创建维护互斥')
    owned=False
    try:
        result=api.WaitForSingleObject(handle,0)
        owned=result in (0,0x80)
        if not owned:raise RuntimeError('该数据目录正在启动或维护')
        yield
    finally:
        if owned:api.ReleaseMutex(handle)
        api.CloseHandle(handle)


@contextmanager
def maintenance(data_dir,operation):
    data=Path(data_dir).resolve()
    with start_mutex(data),file_lock(data/'.local-service'/'lifecycle.lock'):
        marker=data/'.local-service'/'maintenance.json'
        marker.write_text(json.dumps({'operation':operation,'pid':os.getpid(),'active':True}),encoding='utf-8')
        try:yield
        finally:marker.unlink(missing_ok=True)
