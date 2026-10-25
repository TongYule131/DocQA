"""下一阶段N0：审查发现的九项失败及独立安全边界，全部离线合成。"""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.config import Settings
from app.repository import Repository
from app.resource_governance import provider_guard, stamp
from app.knowledge_tasks import KnowledgeTasks, TaskStopped
from app import data_backup, migrations, local_supervisor
from app.query_understanding import build_memory


class Error(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def environment(tmp_path, **options):
    settings = Settings(data_dir=tmp_path, **options)
    repo = Repository(tmp_path / 'docqa.db')
    repo.settings = settings
    repo.initialize()
    return settings, repo


def test_old_completion_cannot_clear_current_half_open_probe(tmp_path):
    settings, repo = environment(tmp_path, active_user_limit=4, circuit_failure_threshold=1)
    old = provider_guard(settings, 'deepseek', Error)
    old.__enter__()
    with pytest.raises(Error):
        with provider_guard(settings, 'deepseek', Error):
            raise Error(503, 'synthetic failure')
    with repo.connect() as db:
        db.execute('UPDATE provider_circuits SET open_until=?', (stamp(-1),))
    probe = provider_guard(settings, 'deepseek', Error)
    probe.__enter__()
    try:
        old.__exit__(None, None, None)
        with pytest.raises(Error) as blocked:
            with provider_guard(settings, 'deepseek', Error):
                pytest.fail('current probe must remain exclusive')
        assert blocked.value.not_sent
    finally:
        probe.__exit__(None, None, None)


def test_local_cancellation_does_not_poison_provider_health(tmp_path):
    settings, repo = environment(tmp_path, circuit_failure_threshold=1)
    with pytest.raises(TaskStopped):
        with provider_guard(settings, 'deepseek', Error):
            raise TaskStopped('cancelled', 'synthetic cancellation', 'cancelled')
    with repo.connect() as db:
        row = db.execute('SELECT failures,open_until FROM provider_circuits').fetchone()
        assert not row or tuple(row) == (0, None)


def test_not_sent_releases_task_budget_but_keeps_monotonic_history(tmp_path):
    settings, repo = environment(tmp_path, calls_user_daily_limit=1)
    with provider_guard(settings, 'deepseek', Error):
        pass
    tasks = KnowledgeTasks(repo)
    job = tasks.create('question', {'route': {'kind': 'SYSTEM'}}, 'first', max_requests=1)
    job = tasks.claim('synthetic-worker', 300, job['id'])
    def denied():
        with provider_guard(settings, 'deepseek', Error):
            pytest.fail('quota must reject')
    with pytest.raises(TaskStopped):
        tasks.call(job, 'generation', denied)
    assert tasks.row(job['id'])['requests_used'] == 0
    with repo.connect() as db:
        row = db.execute('SELECT sequence_no,error_code FROM kb_task_calls').fetchone()
        assert tuple(row) == (1, 'not_sent')
    assert tasks.call(job, 'generation', lambda: ('synthetic', None)) == 'synthetic'
    with repo.connect() as db:
        assert [row[0] for row in db.execute('SELECT sequence_no FROM kb_task_calls ORDER BY sequence_no')] == [1, 2]
    assert tasks.row(job['id'])['requests_used'] == 1


def test_retry_cannot_bypass_queue_admission(tmp_path):
    from tests.test_question_jobs import question_fixture, submit
    env = question_fixture(tmp_path, queue_user_limit=1)
    first, _ = submit(env, key='first')
    with env.repository.connect() as db:
        db.execute("UPDATE question_jobs SET status='failed' WHERE id=?", (first['id'],))
    submit(env, key='second')
    with pytest.raises(HTTPException) as denied:
        env.service.retry_job(first['id'], {'idempotency_key': 'retry-first'})
    assert denied.value.status_code == 429
    with env.repository.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM question_jobs WHERE status='queued'").fetchone()[0] == 1
        assert db.execute('SELECT status FROM question_jobs WHERE id=?', (first['id'],)).fetchone()[0] == 'failed'


def test_backup_accepts_matching_historical_schema_before_upgrade(tmp_path, monkeypatch):
    source = tmp_path / 'v14'
    with monkeypatch.context() as historical:
        historical.setattr(migrations, 'SCHEMA_VERSION', 14)
        Repository(source / 'docqa.db').initialize()
    manifest = data_backup.backup(source, tmp_path / 'backup')
    assert manifest['schema_version'] == 14
    result = data_backup.restore(tmp_path / 'backup', tmp_path / 'restored')
    assert result['schema_version'] == 14 and not result['services_started']
    with Repository(source / 'docqa.db').connect() as db:
        assert migrations.detect_version(db) == 14


def test_restore_rechecks_the_bytes_actually_written(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    environment(source)
    (source / 'asset.txt').write_text('synthetic original', encoding='utf-8')
    snapshot = tmp_path / 'backup'
    data_backup.backup(source, snapshot)
    original = data_backup.shutil.copy2
    def race(src, dst, *args, **kwargs):
        if Path(src).name == 'asset.txt':
            Path(src).write_text('synthetic changed', encoding='utf-8')
        return original(src, dst, *args, **kwargs)
    monkeypatch.setattr(data_backup.shutil, 'copy2', race)
    with pytest.raises(ValueError, match='changed|hash|manifest'):
        data_backup.restore(snapshot, tmp_path / 'restored')
    assert not (tmp_path / 'restored' / 'restore-complete.json').exists()


def test_ordinary_quoted_contrast_does_not_revoke_comparison_memory():
    history = [{'turn': 1, 'question': '比较甲方案与乙方案的区别'},
               {'turn': 2, 'question': '解释“不是检索而是生成”这句话'}]
    assert build_memory(history)['comparison_objects'] == ['甲方案', '乙方案']
    corrected = build_memory(history + [{'turn': 3, 'question': '更正：比较丙方案与丁方案的区别'}])
    assert corrected['comparison_objects'] == ['丙方案', '丁方案']


def test_supervisor_restart_window_survives_supervisor_processes(tmp_path, monkeypatch):
    class Guard:
        def __init__(self, data_dir, role):
            self.data_dir, self.fingerprint = data_dir, 'synthetic'
        def check(self):
            return {}
    starts = []
    def script(name, data_dir, port=None):
        if name == 'start_local.ps1':
            starts.append(name)
        return SimpleNamespace(returncode=0, stdout=b'')
    monkeypatch.setattr(local_supervisor, 'RuntimeGuard', Guard)
    monkeypatch.setattr(local_supervisor, 'roles_ready', lambda *_: False)
    for _ in range(2):
        assert local_supervisor.supervise(tmp_path, max_cycles=4, sleep=lambda _: None, run_script=script) == 3
    assert len(starts) == 3


def test_expired_temporary_base_purges_private_entity_payload(tmp_path):
    from app.product_access import ProductAccess
    from app.knowledge_questions import KnowledgeQuestions
    from app.entity_memory import edit
    from app.attachment_maintenance import purge_temporary_bases
    settings, repo = environment(tmp_path)
    access = ProductAccess(repo, settings, SimpleNamespace(embedding=SimpleNamespace(signature='synthetic')))
    base = access.create_base('synthetic', '', collection_name='synthetic')
    questions = KnowledgeQuestions(access, None)
    conv = questions.create(base['id'], 'conversation', allow_preparing=True)
    edit(questions, conv['id'], ['synthetic private entity'], conv['revision'])
    with repo.connect() as db:
        db.execute("UPDATE knowledge_bases SET kind='temporary',expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (base['id'],))
    assert purge_temporary_bases(tmp_path, repo) == [base['id']]
    with repo.connect() as db:
        assert not db.execute('SELECT 1 FROM conversation_entity_memory WHERE conversation_id=?', (conv['id'],)).fetchone()
