"""真实v16结构的隔离资产→v17；故障回滚、备份恢复和旧登录兼容。"""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3,time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import migrations
from app.main import create_app
from app.product_access import ProductAccess,COOKIE,digest
from app.question_worker import QuestionWorker
from app.repository import Repository,now_iso
from tests.test_profile import picture
from tests.test_question_jobs import question_fixture,LocalRuntime,QUESTION


def snapshot(db):
    return {r[0]:[tuple(row) for row in db.execute('SELECT * FROM "'+r[0]+'" ORDER BY rowid')]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations' ORDER BY name")}


def old_environment(tmp_path):
    with patch.object(migrations,'SCHEMA_VERSION',16):
        env=question_fixture(tmp_path)
        user=ProductAccess(env.repository,env.settings,env.rag.index).create_user('legacy-admin','历史显示名','legacy-password','admin')
        response,_=env.service.submit('qdoc',{'question':QUESTION,'idempotency_key':'legacy-result'});job=response['job']
        assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
        assert env.service.get_job(job['id'])['status']=='succeeded'
        with env.repository.connect() as db:
            db.execute('INSERT INTO product_sessions VALUES (?,?,?,?,?)',(digest('synthetic-old-cookie'),user['id'],'synthetic-old-csrf',int(time.time())+3600,now_iso()))
            db.execute('INSERT INTO product_user_avatars VALUES (?,?,?,?)',(user['id'],picture(),'legacy-avatar-revision',now_iso()))
            assert migrations.validate_schema(db,version=16)==16
            before=snapshot(db)
    return env,user,before


def test_upgrade_preserves_all_old_rows_avatar_sources_and_session_and_is_idempotent(tmp_path):
    env,user,before=old_environment(tmp_path)
    summary=env.repository.initialize();assert summary['from']==16 and summary['to']==migrations.SCHEMA_VERSION and summary['applied']==list(range(17,migrations.SCHEMA_VERSION+1)) and summary['backup']
    with env.repository.connect() as db:
        after=snapshot(db);assert all(after[name]==rows for name,rows in before.items())
        assert tuple(db.execute('SELECT user_id,must_change_password,credential_version FROM product_account_state').fetchone())==(user['id'],0,0)
        assert not db.execute('SELECT 1 FROM product_account_events').fetchone()
    with closing(sqlite3.connect(summary['backup'])) as backup:
        assert migrations.validate_schema(backup,version=16)==16 and snapshot(backup)==before
    assert env.repository.initialize()['applied']==[]
    settings=replace(env.settings,team_mode=True,session_cookie_secure=False)
    with TestClient(create_app(settings)) as client:
        client.cookies.set(COOKIE,'synthetic-old-cookie');client.headers['X-CSRF-Token']='synthetic-old-csrf'
        me=client.get('/api/auth/me').json()['user'];assert me['id']==user['id'] and not me['must_change_password']
        assert client.put('/api/auth/profile',json={'display_name':'升级后名称'}).status_code==200


def test_failed_v17_upgrade_rolls_back_and_backup_restores_in_another_isolated_directory(tmp_path):
    env,user,before=old_environment(tmp_path/'source')
    previous_backups=set((env.settings.data_dir/'db-backups').glob('*.db'))
    migrations.MigrationHook.register(17,lambda:(_ for _ in ()).throw(RuntimeError('synthetic-account-migration-failure')))
    try:
        with pytest.raises(migrations.MigrationError,match='synthetic-account-migration-failure'):env.repository.initialize()
    finally:migrations.MigrationHook.clear()
    with env.repository.connect() as db:assert migrations.validate_schema(db,version=16)==16 and snapshot(db)==before
    # glob不保证创建顺序；精确选本次失败迁移生成的备份，不能误取建库前的空库备份。
    backups=set((env.settings.data_dir/'db-backups').glob('*.db'))-previous_backups;assert len(backups)==1
    failed_backup=backups.pop()
    restored=tmp_path/'restored';restored.mkdir()
    with closing(sqlite3.connect(failed_backup.as_uri()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(restored/'docqa.db')) as target:
        assert migrations.validate_schema(source,version=16)==16 and snapshot(source)==before
        source.backup(target)
    repository=Repository(restored/'docqa.db');assert repository.initialize()['applied']==list(range(17,migrations.SCHEMA_VERSION+1))
    with repository.connect() as db:
        after=snapshot(db);assert all(after[name]==rows for name,rows in before.items())
        assert not db.execute('SELECT 1 FROM product_account_state WHERE must_change_password=1').fetchone()
