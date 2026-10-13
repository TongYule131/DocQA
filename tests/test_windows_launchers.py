"""双 PowerShell 入口真实回归；空密钥、独立目录，禁止触达正式库与模型。"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCRIPT_NAMES = ("local_service_common.ps1", "start_local.ps1", "status_local.ps1", "stop_local.ps1")
SHELL_NAMES = ("powershell.exe", "pwsh.exe")
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows 启停入口")


@pytest.fixture(params=SHELL_NAMES)
def shell(request):
    # 必须显式测系统自带 5.1，不能由 pwsh 优先回退掩盖兼容问题。
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(f"未安装 {request.param}")
    return executable


def isolated_env(data_dir):
    return {
        **os.environ,
        "DOCQA_DATA_DIR": str(data_dir.resolve()),
        "DEEPSEEK_API_KEY": "",
        "EMBEDDING_API_KEY": "",
        "DOCQA_ALLOW_DATABASE_UPGRADE": "0",
        "DOCQA_EXPECTED_CODE_FINGERPRINT": "",
        "DOCQA_DOCLING_BASE_URL": "http://127.0.0.1:9",
        "PYTHONIOENCODING": "utf-8",
    }


def run_script(shell, data_dir, logs_dir, label, script, *arguments):
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = logs_dir / f"{label}.stdout.txt", logs_dir / f"{label}.stderr.txt"
    # 后台 Start-Process 会继承句柄；普通文件避免 PIPE 等待子进程 EOF 挂住。
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / script),
             "-DataDir", str(data_dir), *arguments],
            cwd=ROOT, env=isolated_env(data_dir), stdout=stdout, stderr=stderr, timeout=75,
        )
    result.stdout = stdout_path.read_bytes()
    result.stderr = stderr_path.read_bytes()
    return result


def require_success(result):
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def port_open(port):
    with socket.socket() as connection:
        return connection.connect_ex(("127.0.0.1", port)) == 0


def read_status(shell, data_dir, logs_dir, label):
    result = run_script(shell, data_dir, logs_dir, label, "status_local.ps1", "-AsJson")
    require_success(result)
    try:
        text = result.stdout.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 控制台可继承本机 ANSI 编码；兼容 Windows 5.1 及沿用该控制台的 pwsh。
        text = result.stdout.decode("mbcs")
    records = json.loads(text)
    assert isinstance(records, list)
    return records


def test_scripts_parse_under_each_shell(shell, tmp_path):
    script = tmp_path / "parse-check.ps1"
    # 先使用 ASCII 构造检查脚本，ParseFile 自己按各版本编码规则读取产品脚本。
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$rows = @(foreach ($file in Get-ChildItem -LiteralPath $args[0] -Filter '*.ps1') {\n"
        "  $tokens = $null; $parseErrors = $null\n"
        "  $null = [System.Management.Automation.Language.Parser]::ParseFile("
        "$file.FullName, [ref]$tokens, [ref]$parseErrors)\n"
        "  @{file=$file.Name; errors=@($parseErrors).Count; version=$PSVersionTable.PSVersion.ToString()}\n"
        "})\nConvertTo-Json -InputObject $rows\n"
        "if (@($rows | Where-Object {$_.errors -gt 0}).Count) {exit 1}\n",
        encoding="ascii",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(SCRIPTS)],
        cwd=ROOT, env=isolated_env(tmp_path), capture_output=True, timeout=20,
    )
    require_success(result)
    rows = {row["file"]: row for row in json.loads(result.stdout.decode("utf-8-sig"))}
    assert all(rows[name]["errors"] == 0 for name in SCRIPT_NAMES)
    if Path(shell).name.lower() == "powershell.exe":
        assert all(rows[name]["version"].startswith("5.1.") for name in SCRIPT_NAMES)
    (tmp_path / "parse-result.json").write_bytes(result.stdout)


def test_dry_run_and_empty_status_do_not_create_data(shell, tmp_path):
    data_dir = tmp_path / "missing data 中文"
    result = run_script(shell, data_dir, tmp_path, "dryrun", "start_local.ps1",
                        "-Port", str(unused_port()), "-DryRun")
    require_success(result)
    assert not data_dir.exists()
    assert read_status(shell, data_dir, tmp_path, "empty-status") == []
    assert not data_dir.exists()


def test_environment_data_dir_survives_native_utf8_output(shell, tmp_path):
    data_dir = tmp_path / "environment data 中文"
    check = tmp_path / "unicode-path-check.ps1"
    check.write_text(
        ". (Join-Path $args[0] 'scripts/local_service_common.ps1')\n"
        "$resolved = Resolve-LocalDataDir '' $args[0]\n"
        "$info = Get-LocalRuntimeInfo $args[0] $resolved\n"
        "if ($resolved -cne $env:DOCQA_DATA_DIR -or "
        "$info.data_dir -cne $env:DOCQA_DATA_DIR) {throw 'UTF-8 native output changed data_dir'}\n",
        encoding="ascii",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(check), str(ROOT)],
        cwd=ROOT, env=isolated_env(data_dir), capture_output=True, timeout=20,
    )
    require_success(result)
    assert not data_dir.exists()


def test_three_roles_conflict_and_restart_preserve_data(shell, tmp_path):
    data_dir = tmp_path / "service data 中文"
    port = unused_port()
    records = []
    try:
        for phase in ("initial", "restart"):
            result = run_script(shell, data_dir, tmp_path, f"{phase}-start", "start_local.ps1",
                                "-Port", str(port))
            require_success(result)
            records = read_status(shell, data_dir, tmp_path, f"{phase}-status")
            assert {record["role"] for record in records} == {"web", "parse-worker", "analysis-worker"}
            assert all(all(record[field] for field in (
                "running", "identity_match", "data_dir_match", "ready", "code_match", "schema_match",
            )) for record in records)
            assert all(record["data_dir"] == str(data_dir) for record in records)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                health = client.get("/api/health")
                assert health.status_code == 200
                assert health.json()["runtime"]["data_dir"] == str(data_dir)
                status = client.get("/api/runtime/status")
                assert status.status_code == 200
                assert status.headers["cache-control"] == "no-store"
                assert all(role["ready"] and role["running"] for role in
                           [status.json()["web"], *status.json()["workers"]])
                if phase == "initial":
                    payload = "合成启停测试。\n\n样本数量为12项。\n".encode("utf-8")
                    upload = client.post("/api/documents", files={"file": ("synthetic.txt", payload, "text/plain")})
                    assert upload.status_code == 201
                    doc_id = upload.json()["document"]["id"]
                    parse = client.post(f"/api/documents/{doc_id}/parse", json={})
                    assert parse.status_code == 202
                    task_id = parse.json()["task"]["id"]
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        state = client.get(f"/api/parse-tasks/{task_id}").json()
                        if state["status"] in {"succeeded", "failed", "needs_attention", "cancelled"}:
                            break
                        time.sleep(0.2)
                    assert state["status"] == "succeeded"
                    content = client.get(f"/api/documents/{doc_id}/content").json()
                    assert len(content["blocks"]) == 2
                    conflict_dir = tmp_path / "conflict data 中文"
                    conflict = run_script(shell, conflict_dir, tmp_path, "port-conflict", "start_local.ps1",
                                          "-Port", str(port))
                    assert conflict.returncode != 0
                    assert not (conflict_dir / "docqa.db").exists()
                    assert not list((conflict_dir / ".local-service").glob("*.json"))
                    assert client.get("/api/health").status_code == 200
                else:
                    assert client.get(f"/api/documents/{doc_id}/content").json() == content
                assert client.get(f"/api/documents/{doc_id}/original").content == payload
                with sqlite3.connect((data_dir / "docqa.db").as_uri() + "?mode=ro", uri=True) as db:
                    assert db.execute("SELECT COUNT(*) FROM analysis_calls").fetchone()[0] == 0
                    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            result = run_script(shell, data_dir, tmp_path, f"{phase}-stop", "stop_local.ps1", "-KeepRecords")
            require_success(result)
            assert not port_open(port)
            stopped = read_status(shell, data_dir, tmp_path, f"{phase}-stopped-status")
            assert len(stopped) == 3 and all(not record["running"] and not record["ready"] for record in stopped)
        require_success(run_script(shell, data_dir, tmp_path, "remove-records", "stop_local.ps1"))
        assert read_status(shell, data_dir, tmp_path, "final-status") == []
    finally:
        # 失败也只通过产品归属记录回收本测试环境，绝不按端口杀其他进程。
        cleanup = run_script(shell, data_dir, tmp_path, "cleanup", "stop_local.ps1", "-KeepRecords")
        require_success(cleanup)
        assert not port_open(port)
