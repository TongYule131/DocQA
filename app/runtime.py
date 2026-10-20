"""本地服务的运行身份与兼容性检查；只读本地文件，不访问模型服务。"""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app import migrations

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def process_start_token(pid: int) -> str | None:
    """读取操作系统的进程创建标识；PID 被复用时不能继承旧的就绪状态。"""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                status = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value != 259:
                    return None
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                    return None
                return "windows:" + str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            finally:
                kernel.CloseHandle(handle)
        # Linux 的启动编号与内核时钟标识同样不依赖应用自行填写的时间。
        process = Path("/proc") / str(pid)
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"linux:{boot_id}:{fields[19]}"
    except (OSError, ValueError, IndexError):
        return None


def code_fingerprint() -> str:
    """绑定业务代码、网页和启停入口，文档/缓存变化不要求重启服务。"""
    paths = [p for p in (PROJECT_ROOT / "app").rglob("*")
             if p.is_file() and p.suffix in {".py", ".js", ".css", ".html"}]
    paths += list((PROJECT_ROOT / "scripts").glob("*.ps1"))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class RuntimeMismatch(RuntimeError):
    """静态安全错误，可直接显示；不包含配置凭证或业务正文。"""


class RuntimeGuard:
    def __init__(self, data_dir: Path, role: str):
        self.data_dir = data_dir.resolve()
        self.database_path = self.data_dir / "docqa.db"
        self.role = role
        self.fingerprint = code_fingerprint()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.process_start_token = process_start_token(os.getpid())
        expected = os.getenv("DOCQA_EXPECTED_CODE_FINGERPRINT")
        if expected and expected != self.fingerprint:
            raise RuntimeMismatch("启动期间代码已变更，请停止三个本地角色后重新启动")

    def check_code(self) -> None:
        if code_fingerprint() != self.fingerprint:
            raise RuntimeMismatch("代码已更新，当前服务仍是旧进程；请停止 Web 和两个 worker 后重新启动")

    def prepare_database(self, allow_upgrade: bool = False) -> None:
        self.check_code()
        info = inspect_database(self.data_dir)
        if not info["structure_valid"]:
            raise RuntimeMismatch(info["schema_error"])
        if (info["database_exists"] and info["schema_version"] < migrations.SCHEMA_VERSION
                and not allow_upgrade):
            raise RuntimeMismatch("已有数据库需要升级；请核对目录与备份后使用 -AllowDatabaseUpgrade，"
                                  "或显式设置 DOCQA_ALLOW_DATABASE_UPGRADE=1")

    def check(self) -> dict:
        self.check_code()
        try:
            # mode=ro 避免错误路径悄悄生成空库。版本号正确也必须核对真实表/列/索引。
            with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                version = migrations.validate_schema(db)
        except (sqlite3.Error, migrations.MigrationError) as exc:
            raise RuntimeMismatch("数据库与当前代码不兼容，服务已阻止继续操作；请检查数据库版本和结构") from exc
        return {"role": self.role, "pid": os.getpid(), "data_dir": str(self.data_dir),
                "database_path": str(self.database_path), "schema_version": version,
                "expected_schema_version": migrations.SCHEMA_VERSION,
                "code_fingerprint": self.fingerprint, "started_at": self.started_at,
                "process_start_token": self.process_start_token}

    def service_status(self) -> dict:
        """只读查询角色实际就绪；不启动 worker、不探测外部服务、不领取任务。"""
        current = self.check()
        web = {"role": "web", "pid": os.getpid(), "running": True, "ready": True,
               "code_match": True, "schema_match": True, "data_dir_match": True,
               "detail": "Web 服务就绪"}
        workers = []
        for role in ("parse-worker", "analysis-worker"):
            state = {"role": role, "running": False, "ready": False, "code_match": False,
                     "schema_match": False, "data_dir_match": False,
                     "detail": "未找到就绪记录，请启动对应 worker"}
            path = self.data_dir / ".local-service" / (role + ".ready")
            try:
                if path.stat().st_size > 65536:
                    raise ValueError("invalid-ready-record")
                record = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(record, dict) or record.get("role") != role:
                    raise ValueError("invalid-ready-record")
                pid = record.get("pid")
                token = process_start_token(pid)
                state.update(running=token is not None,
                    code_match=record.get("code_fingerprint") == self.fingerprint,
                    schema_match=type(record.get("schema_version")) is int and
                        record["schema_version"] == migrations.SCHEMA_VERSION,
                    data_dir_match=record.get("data_dir") == str(self.data_dir) and
                        record.get("database_path") == str(self.database_path))
                identity_match = token is not None and token == record.get("process_start_token")
                state["ready"] = identity_match and all(state[key] for key in
                    ("code_match", "schema_match", "data_dir_match"))
                if state["ready"]:
                    state["detail"] = "worker 已就绪"
                elif not state["running"]:
                    state["detail"] = "worker 已退出，请启动对应 worker"
                elif not identity_match:
                    state["detail"] = "进程身份无法确认，请重新启动对应 worker"
                else:
                    state["detail"] = "代码、数据库或目录不匹配，请整体重启三个角色"
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError):
                state["detail"] = "就绪记录不可读取，请检查并重新启动对应 worker"
            workers.append(state)
        return {"web": web, "workers": workers, "schema_version": current["schema_version"],
                "expected_schema_version": migrations.SCHEMA_VERSION}

    def mark_ready(self) -> None:
        state = self.check()
        directory = self.data_dir / ".local-service"
        directory.mkdir(parents=True, exist_ok=True)
        # ready 与管理脚本的进程记录分开，只有角色自身初始化完成才能发布就绪证据。
        destination = directory / (self.role + ".ready")
        temporary = directory / (self.role + f".{os.getpid()}.ready.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(temporary, destination)
                return
            except OSError as exc:
                # Windows 的状态读取/文件扫描可能短暂阻止替换；保持原子发布，
                # 仅等待本地文件共享冲突，持续拒绝或其他错误仍使启动失败。
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
                self.check_code()


def inspect_database(data_dir: Path) -> dict:
    """供三个启停脚本共用的只读预检，不创建库，也不加载 .env 中的密钥。"""
    database_path = data_dir.resolve() / "docqa.db"
    info = {"code_fingerprint": code_fingerprint(), "expected_schema_version": migrations.SCHEMA_VERSION,
            "data_dir": str(data_dir.resolve()), "database_exists": database_path.exists(),
            "schema_version": 0, "structure_valid": True, "schema_error": None}
    if database_path.exists():
        try:
            with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)) as db:
                current = migrations.detect_version(db)
                info["schema_version"] = current
                if current > migrations.SCHEMA_VERSION:
                    raise migrations.MigrationError("数据库高于当前代码支持的版本")
                migrations.validate_schema(db, version=current)
        except (sqlite3.Error, migrations.MigrationError):
            info.update(structure_valid=False, schema_error="数据库版本登记或结构不兼容，请核对代码与备份")
    return info


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DocQA 本地运行身份与数据库只读预检")
    parser.add_argument("--data-dir", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(inspect_database(arguments.data_dir), ensure_ascii=False))
