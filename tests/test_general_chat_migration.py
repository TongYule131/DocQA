"""隔离v17→v18：旧列、外键、登录及资产保持，失败回滚和新目录恢复。"""
from contextlib import closing
import json
import sqlite3
from unittest.mock import patch

import pytest

from app import migrations
from app.data_backup import backup, restore
from app.product_access import ProductAccess
from app.repository import Repository, now_iso
from tests.test_question_jobs import question_fixture


def columns_snapshot(db):
    result = {}
    for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'"):
        columns = [r[1] for r in db.execute('PRAGMA table_info("' + table + '")')]
        result[table] = (columns, [tuple(r) for r in db.execute('SELECT * FROM "' + table + '" ORDER BY rowid')])
    return result


def old_v17(path):
    with patch.object(migrations, 'SCHEMA_VERSION', 17):
        env = question_fixture(path)
        access = ProductAccess(env.repository, env.settings, env.rag.index)
        base = access.create_base('旧资料范围', '')
        stamp = now_iso()
        with env.repository.connect() as db:
            db.execute('INSERT INTO kb_conversations(id,base_id,title,request_key,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                       ('old-conv', base['id'], '旧会话', 'old-key', stamp, stamp))
            db.execute("INSERT INTO kb_tasks(id,kind,base_id,request_key,input_json,input_hash,status,max_requests,created_at,updated_at) VALUES (?,'question',?,?,?,?,'succeeded',?,?,?)",
                       ('old-task', base['id'], 'old-task-key', '{"question":"旧问题"}', 'historical-synthetic', 4, stamp, stamp))
            db.execute('INSERT INTO kb_conversation_turns VALUES (?,?,?)', ('old-conv', 1, 'old-task'))
            db.execute("INSERT INTO chat_submissions(id,conversation_id,request_key,fingerprint,question,document_ids_json,waiting_jobs_json,memory_epoch,status,task_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,0,'submitted',?,?,?)",
                       ('old-sub', 'old-conv', 'old-sub-key', 'old-fingerprint', '旧问题', '[]', '[]', 'old-task', stamp, stamp))
            before = columns_snapshot(db)
            assert migrations.validate_schema(db, version=17) == 17
    return env, before


def assert_old_fields(db, before):
    for table, (columns, rows) in before.items():
        selected = ','.join('"' + c + '"' for c in columns)
        assert [tuple(r) for r in db.execute(f'SELECT {selected} FROM "{table}" ORDER BY rowid')] == rows, table
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_v18_preserves_all_old_fields_and_allows_real_base_less_conversation(tmp_path):
    env, before = old_v17(tmp_path)
    migrated = env.repository.initialize()
    assert migrated['from'] == 17 and migrated['to'] == migrations.SCHEMA_VERSION and migrated['applied'] == list(range(18,migrations.SCHEMA_VERSION+1))
    with env.repository.connect() as db:
        assert_old_fields(db, before)
        assert migrations.validate_schema(db) == migrations.SCHEMA_VERSION
        assert db.execute('SELECT resource_mode FROM kb_conversations WHERE id=?', ('old-conv',)).fetchone()[0] is None
        stamp = now_iso()
        db.execute("INSERT INTO kb_conversations(id,base_id,title,request_key,created_at,updated_at,resource_mode) VALUES (?,NULL,?,?,?,?, 'none')",
                   ('general-conv', '普通会话', 'general-key', stamp, stamp))
    assert env.repository.initialize()['applied'] == []
    with closing(sqlite3.connect(migrated['backup'])) as db:
        assert migrations.validate_schema(db, version=17) == 17
        assert_old_fields(db, before)


def test_v18_failure_rolls_back_and_backup_restores_without_overwriting_source(tmp_path):
    env, before = old_v17(tmp_path / 'old')
    migrations.MigrationHook.register(18, lambda: (_ for _ in ()).throw(RuntimeError('synthetic-v18-failure')))
    try:
        with pytest.raises(migrations.MigrationError, match='synthetic-v18-failure'):
            env.repository.initialize()
    finally:
        migrations.MigrationHook.clear()
    with env.repository.connect() as db:
        assert migrations.validate_schema(db, version=17) == 17
        assert_old_fields(db, before)
    backup(env.settings.data_dir, tmp_path / 'backup')
    restore(tmp_path / 'backup', tmp_path / 'restored')
    restored = Repository(tmp_path / 'restored/docqa.db')
    assert restored.initialize()['applied'] == list(range(18,migrations.SCHEMA_VERSION+1))
    with restored.connect() as db:
        assert_old_fields(db, before)
    with env.repository.connect() as db:
        assert migrations.validate_schema(db, version=17) == 17
