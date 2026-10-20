"""运行代码/数据库一致性反例；全部使用独立目录，不访问在线模型。"""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import migrations
from app.config import Settings
from app.main import create_app
from app.repository import Repository
from app.runtime import RuntimeGuard, RuntimeMismatch, inspect_database
from tests.test_migration_and_versions import build_legacy_db
from tests.test_stable_review_regressions import setup_job
from tests.test_analysis import StubModel
from app.analysis_worker import AnalysisWorker
from app.parse_worker import ParseWorker


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def initialized(tmp_path):
    repo = Repository(tmp_path / 'docqa.db')
    repo.initialize()
    return repo


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_ready_publication_waits_for_transient_windows_lock_atomically(tmp_path, monkeypatch, winerror):
    initialized(tmp_path)
    runtime = RuntimeGuard(tmp_path, 'web')
    directory = tmp_path / '.local-service'
    directory.mkdir()
    destination = directory / 'web.ready'
    destination.write_text('old-complete-record', encoding='utf-8')
    replace = os.replace
    attempts, sleeps = [], []

    def locked_replace(source, target):
        attempts.append((source, target))
        assert destination.read_text(encoding='utf-8') == 'old-complete-record'
        if len(attempts) < 3:
            error = PermissionError('simulated Windows sharing conflict')
            error.winerror = winerror
            raise error
        replace(source, target)

    monkeypatch.setattr('app.runtime.os.replace', locked_replace)
    monkeypatch.setattr('app.runtime.time.sleep', sleeps.append)
    runtime.mark_ready()
    assert len(attempts) == 3 and sleeps == [0.05, 0.1]
    assert json.loads(destination.read_text(encoding='utf-8'))['pid'] == os.getpid()
    assert not list(directory.glob('*.tmp'))


@pytest.mark.parametrize('winerror,attempts_expected', [(5, 6), (32, 6), (33, 6), (87, 1), (None, 1)])
def test_ready_publication_fails_closed_on_persistent_or_unrelated_errors(
        tmp_path, monkeypatch, winerror, attempts_expected):
    initialized(tmp_path)
    runtime = RuntimeGuard(tmp_path, 'web')
    attempts, sleeps = [], []

    def denied_replace(source, target):
        attempts.append((source, target))
        error = PermissionError('simulated persistent or unrelated failure')
        if winerror is not None:
            error.winerror = winerror
        raise error

    monkeypatch.setattr('app.runtime.os.replace', denied_replace)
    monkeypatch.setattr('app.runtime.time.sleep', sleeps.append)
    with pytest.raises(PermissionError):
        runtime.mark_ready()
    assert len(attempts) == attempts_expected
    assert len(sleeps) == attempts_expected - 1
    assert sum(sleeps) <= 0.75
    assert not (tmp_path / '.local-service/web.ready').exists()


@pytest.mark.parametrize('damage', ['future', 'table', 'column', 'index', 'index_shape', 'gap',
                                    'primary_key', 'foreign_key', 'unique_constraint'])
def test_incompatible_database_rejected_without_repair_or_writes(tmp_path, damage):
    repo = initialized(tmp_path)
    with sqlite3.connect(repo.path) as db:
        if damage == 'future':
            db.execute("INSERT INTO schema_migrations VALUES (?, 'synthetic-future')", (migrations.SCHEMA_VERSION + 1,))
        elif damage == 'table':
            db.execute('DROP TABLE analysis_request_keys')
        elif damage == 'column':
            db.execute('ALTER TABLE analysis_request_keys DROP COLUMN request_fingerprint')
        elif damage == 'index':
            db.execute('DROP INDEX ux_analysis_jobs_active')
        elif damage == 'index_shape':
            db.execute('DROP INDEX ux_analysis_jobs_active')
            db.execute('CREATE INDEX ux_analysis_jobs_active ON analysis_jobs(document_id, kind)')
        elif damage == 'gap':
            db.execute('DELETE FROM schema_migrations WHERE version=3')
        elif damage in {'primary_key', 'foreign_key'}:
            db.execute('DROP TABLE analysis_request_keys')
            references = ' REFERENCES documents(id) ON DELETE CASCADE' if damage != 'foreign_key' else ''
            primary_key = ', PRIMARY KEY(document_id, idempotency_key)' if damage != 'primary_key' else ''
            db.execute(f'''CREATE TABLE analysis_request_keys (
                document_id TEXT NOT NULL{references}, idempotency_key TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
                request_fingerprint TEXT NOT NULL{primary_key})''')
        else:
            definition = db.execute("SELECT sql FROM sqlite_master WHERE name='blocks'").fetchone()[0]
            indexes = [row[0] for row in db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='blocks' AND sql IS NOT NULL")]
            db.execute('DROP TABLE blocks')
            db.execute(definition.replace(',\n    UNIQUE (parse_version_id, order_index)', ''))
            for definition in indexes:
                db.execute(definition)
    before = file_hash(repo.path)
    backups_before = set((tmp_path / 'db-backups').glob('*.db'))
    with pytest.raises(migrations.MigrationError):
        repo.initialize()
    assert file_hash(repo.path) == before
    assert set((tmp_path / 'db-backups').glob('*.db')) == backups_before


def test_health_reports_actual_database_and_running_code(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        response = client.get('/api/health')
        assert response.status_code == 200
        runtime = response.json()['runtime']
        assert runtime['data_dir'] == str(tmp_path.resolve())
        assert runtime['database_path'] == str((tmp_path / 'docqa.db').resolve())
        assert runtime['schema_version'] == runtime['expected_schema_version'] == migrations.SCHEMA_VERSION
        ready = json.loads((tmp_path / '.local-service/web.ready').read_text(encoding='utf-8'))
        assert ready == runtime


def test_page_assets_follow_running_code_and_do_not_reuse_old_cache(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        fingerprint = client.get('/api/health').json()['runtime']['code_fingerprint']
        page = client.get('/')
        assert page.headers['cache-control'] == 'no-store'
        for asset in ['app.js', 'style.css']:
            path = f'/static/{asset}?v={fingerprint}'
            assert f'"{path}"' in page.text
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
        assert 'renderLegacyChunks' in client.get(f'/static/app.js?v={fingerprint}').text


@pytest.mark.parametrize('damage', ['future', 'table'])
def test_database_changed_while_running_blocks_upload_before_any_side_effect(tmp_path, damage):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        with sqlite3.connect(tmp_path / 'docqa.db') as db:
            if damage == 'future':
                db.execute("INSERT INTO schema_migrations VALUES (?, 'synthetic-future')", (migrations.SCHEMA_VERSION + 1,))
            else:
                db.execute('DROP TABLE analysis_request_keys')
        assert client.get('/api/health').status_code == 503
        response = client.post('/api/documents', files={'file': ('synthetic.txt', b'offline')})
        assert response.status_code == 503
        assert response.json()['code'] == 'runtime_mismatch'
        assert not list((tmp_path / 'uploads').iterdir())
        with sqlite3.connect(tmp_path / 'docqa.db') as db:
            assert db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 0


def test_source_change_rejects_old_web_process(tmp_path, monkeypatch):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        monkeypatch.setattr('app.runtime.code_fingerprint', lambda: 'synthetic-new-code')
        response = client.get('/api/documents')
        assert response.status_code == 503
        assert '重' in response.json()['detail']


@pytest.mark.parametrize('role', ['parse-worker', 'analysis-worker'])
def test_old_worker_does_not_claim_or_charge_after_code_change(tmp_path, monkeypatch, role):
    if role == 'analysis-worker':
        settings, repo, service, _, job = setup_job(tmp_path, 'extraction')
        model = StubModel([])
        worker = AnalysisWorker(settings, repo, model=model)
    else:
        repo = initialized(tmp_path)
        worker = ParseWorker(Settings(data_dir=tmp_path), repo)
    monkeypatch.setattr('app.runtime.code_fingerprint', lambda: 'synthetic-new-code')
    with pytest.raises(RuntimeMismatch):
        worker.run_forever(max_tasks=1)
    if role == 'analysis-worker':
        assert not model.calls
        current = repo.get_analysis_job(job.id)
        assert current.status == 'queued' and current.requests_used == 0


def test_startup_fingerprint_race_rejected_before_creating_database(tmp_path, monkeypatch):
    monkeypatch.setenv('DOCQA_EXPECTED_CODE_FINGERPRINT', 'synthetic-other-build')
    with pytest.raises(RuntimeMismatch):
        create_app(Settings(data_dir=tmp_path))
    assert not (tmp_path / 'docqa.db').exists()


def test_read_only_preflight_does_not_create_database(tmp_path):
    missing = tmp_path / 'missing-data'
    info = inspect_database(missing)
    assert not info['database_exists'] and info['structure_valid']
    assert not missing.exists()


def test_legacy_preview_reads_existing_chunks_without_manufacturing_sources(tmp_path):
    path = tmp_path / 'docqa.db'
    build_legacy_db(path, provider_signature='synthetic-provider')
    with TestClient(create_app(Settings(data_dir=tmp_path, deepseek_api_key='offline-fake', allow_database_upgrade=True))) as client:
        with sqlite3.connect(path) as db:
            before = list(db.execute('SELECT * FROM parse_versions'))
            old_chunks = list(db.execute('SELECT id, text FROM chunks ORDER BY order_index'))
        first = client.get('/api/documents/legacy-doc/content?limit=1').json()
        second = client.get('/api/documents/legacy-doc/content?limit=1&offset=1').json()
        assert first['content_mode'] == 'legacy_chunks'
        assert first['blocks'] == []
        assert first['returned'] == second['returned'] == 1 and first['total'] == 2
        shown = first['legacy_chunks'] + second['legacy_chunks']
        assert [(c['id'], c['text']) for c in shown] == old_chunks
        # 原有摘要/提取前置仍拒绝无结构化来源的 legacy 版本。
        response = client.post('/api/documents/legacy-doc/analysis-plan?kind=summary')
        assert response.status_code == 200
        assert not response.json()['executable']
        assert response.json()['blocked_reason'] == 'legacy_version_unsupported'
        with sqlite3.connect(path) as db:
            assert before == list(db.execute('SELECT * FROM parse_versions'))


def test_direct_web_requires_explicit_existing_database_upgrade(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(migrations, 'SCHEMA_VERSION', 2)
        Repository(tmp_path / 'docqa.db').initialize()
    before = file_hash(tmp_path / 'docqa.db')
    with pytest.raises(RuntimeMismatch):
        with TestClient(create_app(Settings(data_dir=tmp_path))):
            pass
    assert file_hash(tmp_path / 'docqa.db') == before
    with TestClient(create_app(Settings(data_dir=tmp_path, allow_database_upgrade=True))) as client:
        assert client.get('/api/health').json()['runtime']['schema_version'] == migrations.SCHEMA_VERSION


@pytest.mark.parametrize('worker_class', [ParseWorker, AnalysisWorker])
def test_direct_worker_requires_explicit_existing_database_upgrade(tmp_path, monkeypatch, worker_class):
    with monkeypatch.context() as patch:
        patch.setattr(migrations, 'SCHEMA_VERSION', 2)
        Repository(tmp_path / 'docqa.db').initialize()
    before = file_hash(tmp_path / 'docqa.db')
    worker = worker_class(Settings(data_dir=tmp_path))
    with pytest.raises(RuntimeMismatch):
        worker.run_forever(max_tasks=1)
    assert file_hash(tmp_path / 'docqa.db') == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows 启动入口')
@pytest.mark.parametrize('case', ['future', 'old_requires_flag'])
def test_start_script_refuses_incompatible_or_unapproved_existing_database(tmp_path, monkeypatch, case):
    path = tmp_path / 'docqa.db'
    if case == 'old_requires_flag':
        with monkeypatch.context() as patch:
            patch.setattr(migrations, 'SCHEMA_VERSION', 2)
            Repository(path).initialize()
    else:
        repo = initialized(tmp_path)
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO schema_migrations VALUES (?, 'synthetic-future')", (migrations.SCHEMA_VERSION + 1,))
    before = file_hash(path)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    shell = shutil.which('pwsh') or shutil.which('powershell')
    environment = {**os.environ, 'DEEPSEEK_API_KEY': '', 'EMBEDDING_API_KEY': ''}
    result = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        str(Path('scripts/start_local.ps1').resolve()), '-DataDir', str(tmp_path), '-Port', str(port)],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, timeout=30)
    assert result.returncode != 0
    assert file_hash(path) == before
    assert not list((tmp_path / '.local-service').glob('*.json'))
