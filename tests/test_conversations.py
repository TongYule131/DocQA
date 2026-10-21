"""会话归属、并发、记忆预算与原任务恢复的真实SQLite/HTTP离线验证。"""
import json
import sqlite3
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import migrations
from app.conversation_memory import MEMORY_RULE, MEMORY_VERSION, valid_memory
from app.conversations import Conversations
from app.product_access import ProductAccess
from app.rag_prompts import build_task_system_prompt
from app.main import create_app
from app.question_worker import QuestionWorker
from tests.test_product_access import team, publish
from tests.test_question_jobs import QUESTION, TEXT, REPLY, LocalRuntime, question_fixture


def conversation(team):
    env, users, clients = team
    base = publish(clients, users)
    response = clients['alice'].post('/api/conversations', json={'document_id': 'qdoc', 'idempotency_key': 'create-one'})
    assert response.status_code == 201, response.text
    return env, users, clients, base, response.json()


def turn(client, identifier, key='one', question=QUESTION):
    return client.post(f'/api/conversations/{identifier}/turns', json={'question': question, 'idempotency_key': key})


def run(env, job):
    assert QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job['id'])
    assert env.service.get_job(job['id'])['status'] == 'succeeded'


def test_conversation_owner_default_private_creation_replay_and_zero_call(team):
    env, _, clients, _, c = conversation(team)
    alice = clients['alice']
    again = alice.post('/api/conversations', json={'document_id': 'qdoc', 'idempotency_key': 'create-one'})
    assert again.json()['id'] == c['id']
    assert alice.get(f'/api/conversations/{c["id"]}').json()['turns'] == []
    for name in ['bob', 'admin']:
        assert clients[name].get('/api/conversations').json() == []
        assert clients[name].get(f'/api/conversations/{c["id"]}').status_code == 404
        assert turn(clients[name], c['id']).status_code == 404
    assert alice.post('/api/conversations', json={'document_id': 'qdoc', 'idempotency_key': 'other'}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert not env.embedding.calls and not env.model.calls


def test_turn_atomic_single_active_and_duplicate_post(team):
    env, _, clients, _, c = conversation(team)
    alice = clients['alice']
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: turn(alice, c['id']), range(2)))
    assert [r.status_code for r in responses] == [202, 202]
    first = responses[0].json()['job']
    assert responses[1].json()['job']['id'] == first['id']
    assert turn(alice, c['id'], 'two').status_code == 409
    assert turn(alice, c['id'], question='另一个问题').status_code == 409
    assert len(alice.get(f'/api/conversations/{c["id"]}').json()['turns']) == 1
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM question_jobs WHERE conversation_id=?', (c['id'],)).fetchone()[0] == 1
    assert not env.embedding.calls and not env.model.calls


def test_memory_carries_only_bounded_user_questions_and_notes_then_clear_disable(team):
    env, _, clients, _, c = conversation(team)
    alice = clients['alice']; path = f'/api/conversations/{c["id"]}'
    c = alice.patch(path, json={'expected_revision': c['revision'], 'memory_notes': '请用中文，核对“全书”的范围。'}).json()
    first = turn(alice, c['id']).json()['job']; run(env, first)
    second = turn(alice, c['id'], 'two', '它一共有多少个部分？').json()['job']; run(env, second)
    system, raw = env.model.calls[-1]; prompt = json.loads(raw); memory = prompt['conversation_context']
    assert MEMORY_RULE in system and prompt['question'] == '它一共有多少个部分？'
    assert memory['recent_questions'] == [QUESTION]
    assert memory['notes'] == '请用中文，核对“全书”的范围。'
    assert valid_memory(memory) and set(memory) == {'version', 'notes', 'recent_questions'}
    assert QUESTION in env.embedding.calls[-1][0]
    assert len(system) + len(raw) <= env.settings.question_input_max_chars
    detail = alice.get(path).json(); assert len(detail['turns']) == 2
    cleared = alice.patch(path, json={'expected_revision': detail['revision'], 'clear_memory': True}).json()
    assert cleared['memory']['context']['recent_questions'] == [] and cleared['memory']['context']['notes'] == ''
    assert len(cleared['turns']) == 2  # 清除记忆不删除可核对历史。
    off = alice.patch(path, json={'expected_revision': cleared['revision'], 'memory_enabled': False}).json()
    third = turn(alice, c['id'], 'three').json()['job']; run(env, third)
    assert 'conversation_context' not in json.loads(env.model.calls[-1][1])
    assert MEMORY_RULE not in env.model.calls[-1][0]
    assert alice.patch(path, json={'expected_revision': off['revision'], 'title': '过时修改'}).status_code == 409
    detail = alice.get(path).json()
    on = alice.patch(path, json={'expected_revision': detail['revision'], 'memory_enabled': True}).json()
    assert on['memory']['context']['recent_questions'] == []  # 重新开启不会悄悄发送关闭期间的记录。


def test_active_memory_change_refused_archive_cancels_and_restore_can_continue(team):
    env, _, clients, _, c = conversation(team)
    alice = clients['alice']; path = f'/api/conversations/{c["id"]}'
    job = turn(alice, c['id']).json()['job']; detail = alice.get(path).json()
    assert alice.patch(path, json={'expected_revision': detail['revision'], 'clear_memory': True}).status_code == 409
    archived = alice.patch(path, json={'expected_revision': detail['revision'], 'archived': True}).json()
    assert archived['archived'] and archived['turns'][0]['job']['status'] == 'cancelled'
    assert alice.get('/api/conversations').json() == []
    assert alice.get('/api/conversations?archived=true').json()[0]['id'] == c['id']
    assert turn(alice, c['id'], 'two').status_code == 409
    assert not QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job['id'])
    restored = alice.patch(path, json={'expected_revision': archived['revision'], 'archived': False, 'title': '保留的会话'}).json()
    assert restored['title'] == '保留的会话'
    assert turn(alice, c['id'], 'two').status_code == 202
    assert not env.embedding.calls and not env.model.calls


def test_revocation_blocks_conversation_history_tasks_results_and_new_calls(team):
    env, users, clients, base, c = conversation(team)
    alice = clients['alice']; first = turn(alice, c['id']).json()['job']; run(env, first)
    result = env.service.get_job(first['id'])['result_id']; pending = turn(alice, c['id'], 'two').json()['job']
    before = len(env.embedding.calls), len(env.model.calls)
    clients['admin'].put(f'/api/knowledge-bases/{base["id"]}/members/{users["alice"]["id"]}', json={'enabled': False})
    assert alice.get('/api/conversations').json() == []
    assert alice.get(f'/api/conversations/{c["id"]}').status_code == 404
    assert alice.get(f'/api/question-results/{result}/export').status_code == 404
    assert not QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(pending['id'])
    assert (len(env.embedding.calls), len(env.model.calls)) == before


def test_retry_cannot_reintroduce_cleared_memory_or_overlap_new_turn(team):
    env, _, clients, _, c = conversation(team)
    alice = clients['alice']; path = f'/api/conversations/{c["id"]}'
    old = turn(alice, c['id']).json()['job']
    with env.repository.connect() as db:
        db.execute("UPDATE question_jobs SET status='failed' WHERE id=?", (old['id'],))
    new = turn(alice, c['id'], 'two').json()['job']
    assert alice.post(f'/api/question-jobs/{old["id"]}/retry', json={'idempotency_key': 'retry-overlap'}).status_code == 409
    alice.post(f'/api/question-jobs/{new["id"]}/cancel')
    detail = alice.get(path).json(); alice.patch(path, json={'expected_revision': detail['revision'], 'clear_memory': True})
    assert alice.post(f'/api/question-jobs/{old["id"]}/retry', json={'idempotency_key': 'retry-stale-memory'}).status_code == 409
    assert not env.embedding.calls and not env.model.calls


def test_deleted_conversation_cannot_leak_through_legacy_history_or_exports(team):
    env, _, clients, _, c = conversation(team)
    alice = clients['alice']; path = f'/api/conversations/{c["id"]}'
    job = turn(alice, c['id']).json()['job']; run(env, job)
    result = env.service.get_job(job['id'])['result_id']; detail = alice.get(path).json()
    assert alice.patch(path, json={'expected_revision': detail['revision'], 'deleted': True}).json() == {'deleted': True}
    assert alice.get(path).status_code == 404
    assert alice.get('/api/documents/qdoc/question-jobs').json() == []
    assert alice.get(f'/api/question-jobs/{job["id"]}').status_code == 404
    assert alice.get(f'/api/question-results/{result}/export').status_code == 404


def test_app_restart_reads_same_conversation_results_without_new_generation(team):
    env, _, clients, _, c = conversation(team)
    job = turn(clients['alice'], c['id']).json()['job']; run(env, job)
    expected = clients['alice'].get(f'/api/conversations/{c["id"]}').json()
    with TestClient(create_app(env.settings)) as reopened:
        reopened.cookies.update(clients['alice'].cookies)
        assert reopened.get(f'/api/conversations/{c["id"]}').json() == expected
    assert len(env.model.calls) == 1 and len(env.embedding.calls) == 1


def test_v7_migration_rolls_back_and_preserves_existing_job_columns(tmp_path):
    path = tmp_path / 'docqa.db'
    with sqlite3.connect(path, isolation_level=None) as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        for v in range(1, 7):
            migrations.MIGRATIONS[v](db); db.execute('INSERT INTO schema_migrations VALUES (?,?)', (v, 'synthetic'))
        shapes = list(db.execute('PRAGMA table_info(question_jobs)'))
        migrations.MigrationHook.register(7, lambda: (_ for _ in ()).throw(RuntimeError('synthetic failure')))
        try:
            with pytest.raises(migrations.MigrationError): migrations.run_migrations(db, db_path=path, backup_dir=tmp_path/'backups')
        finally: migrations.MigrationHook.clear()
        assert migrations.detect_version(db) == 6 and list(db.execute('PRAGMA table_info(question_jobs)')) == shapes
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='conversations'").fetchone() is None
        assert migrations.run_migrations(db)['applied'] == list(range(7, migrations.SCHEMA_VERSION + 1))
        assert migrations.validate_schema(db) == migrations.SCHEMA_VERSION


def test_revocation_during_snapshot_capture_cannot_enqueue_an_orphan_job(team, monkeypatch):
    env, users, clients, base, c = conversation(team)
    rag = clients['alice'].app.state.question_service.rag
    capture = rag.capture_snapshot
    def revoke(*args, **kwargs):
        frozen = capture(*args, **kwargs)
        clients['admin'].put(f'/api/knowledge-bases/{base["id"]}/members/{users["alice"]["id"]}', json={'enabled': False})
        return frozen
    monkeypatch.setattr(rag, 'capture_snapshot', revoke)
    assert turn(clients['alice'], c['id']).status_code == 409
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_turns').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM question_jobs').fetchone()[0] == 0
    assert not env.embedding.calls and not env.model.calls


def test_archive_during_generation_prevents_result_publication_and_preserves_charge(team):
    env, _, clients, _, c = conversation(team)
    alice = clients['alice']; path = f'/api/conversations/{c["id"]}'
    job = turn(alice, c['id']).json()['job']
    def archive(system, prompt):
        detail = alice.get(path).json()
        assert alice.patch(path, json={'expected_revision': detail['revision'], 'archived': True}).status_code == 200
        return REPLY
    env.model.handler = archive
    assert QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job['id'])
    closed = env.service.get_job(job['id'])
    assert closed['status'] == 'cancelled' and closed['result_id'] is None and closed['requests_used'] == 2
    assert closed['calls'][-1]['status'] == 'uncertain'


def test_memory_cost_is_included_before_evidence_and_policy_change_stops_zero_call(tmp_path):
    env = question_fixture(tmp_path)
    memory = {'version': MEMORY_VERSION, 'notes': '条件' * 400, 'recent_questions': ['此前的问题' * 100] * 3}
    assert valid_memory(memory)
    system = build_task_system_prompt(include_table_structure=False)
    before = env.rag._context_cap(system, QUESTION, '资料')
    after = env.rag._context_cap(system + MEMORY_RULE, QUESTION, '资料', memory=memory)
    assert after < before
    assert not valid_memory({**memory, 'recent_questions': ['a' * 601]})
    assert not env.embedding.calls and not env.model.calls
    service = Conversations(ProductAccess(env.repository, env.settings, env.rag.index), env.service)
    c = service.create('qdoc', 'create-policy-case')
    submitted, _ = service.submit(c['id'], SimpleNamespace(question=QUESTION, idempotency_key='turn-one'), 'synthetic-policy-request')
    job = submitted['job']
    with env.repository.connect() as db:
        frozen = json.loads(db.execute('SELECT snapshot_json FROM question_jobs WHERE id=?', (job['id'],)).fetchone()[0])
        frozen['conversation_policy_signature'] = 'stale-policy'
        db.execute('UPDATE question_jobs SET snapshot_json=? WHERE id=?', (json.dumps(frozen), job['id']))
    assert QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job['id'])
    failed = env.service.get_job(job['id'])
    assert failed['status'] == 'failed' and failed['error_code'] == 'conversation_policy_changed'
    assert failed['requests_used'] == 0 and not env.embedding.calls and not env.model.calls
