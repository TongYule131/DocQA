"""真实启动入口在本地和已启用团队登录的隔离库上均能就绪。"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from app.repository import Repository


@pytest.mark.skipif(os.name != 'nt', reason='Windows 启动入口')
@pytest.mark.parametrize('team_enabled', [False, True])
def test_start_script_accepts_protected_health_without_exposing_runtime(tmp_path, team_enabled):
    repository = Repository(tmp_path / 'docqa.db')
    repository.initialize()
    with repository.connect() as db:
        db.execute('UPDATE product_installation SET team_enabled=? WHERE singleton=1', (int(team_enabled),))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    shell = shutil.which('pwsh') or shutil.which('powershell')
    prefix = [shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File']
    environment = {**os.environ, 'DOCQA_DATA_DIR': str(tmp_path), 'DOCQA_TEAM_MODE': '0',
        'DEEPSEEK_API_KEY': '', 'EMBEDDING_API_KEY': ''}
    try:
        # Windows 后台子进程可继承管道句柄；使用日志文件，避免等待 EOF 将成功启动误判为超时。
        start_log = tmp_path / 'start.log'
        with start_log.open('wb') as log:
            started = subprocess.run(prefix + [str(root / 'scripts/start_local.ps1'), '-DataDir', str(tmp_path),
                '-Port', str(port), '-NoParseWorker', '-NoAnalysisWorker'], cwd=root, env=environment,
                stdout=log, stderr=subprocess.STDOUT, timeout=30)
        assert started.returncode == 0, start_log.read_text(encoding='utf-8', errors='replace')
        with urlopen(f'http://127.0.0.1:{port}/api/auth/me', timeout=5) as response:
            state = json.load(response)
        assert state['team_mode'] is team_enabled and state['user'] is None
        if team_enabled:
            with pytest.raises(HTTPError) as error:
                urlopen(f'http://127.0.0.1:{port}/api/health', timeout=5)
            assert error.value.code == 401
        else:
            with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=5) as response:
                assert json.load(response)['runtime']['data_dir'] == str(tmp_path)
    finally:
        with (tmp_path / 'stop.log').open('wb') as log:
            subprocess.run(prefix + [str(root / 'scripts/stop_local.ps1'), '-DataDir', str(tmp_path), '-Force'],
                cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=30)
