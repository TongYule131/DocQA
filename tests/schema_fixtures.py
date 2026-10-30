"""旧版本夹具撤销v18／v17／v16增量，禁止只改迁移登记伪造历史schema。"""

def remove_v18(db):
    # 此处的分析旧库夹具不含知识会话；从真实v17迁移生成DDL，撤销可空base和资料模式。
    import sqlite3
    from contextlib import closing
    from app.migrations import MIGRATIONS
    columns={r[1] for r in db.execute('PRAGMA table_info(kb_conversations)')}
    if 'resource_mode' not in columns:return
    assert db.execute('SELECT 1 FROM kb_conversations LIMIT 1').fetchone() is None
    with closing(sqlite3.connect(':memory:')) as baseline:
        for version in range(1,18):MIGRATIONS[version](baseline)
        sql=baseline.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='kb_conversations'").fetchone()[0]
        indexes=[r[0] for r in baseline.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='kb_conversations' AND sql IS NOT NULL")]
    db.execute('DROP TABLE kb_conversations');db.execute(sql)
    for index in indexes:db.execute(index)
    db.execute('ALTER TABLE chat_submissions DROP COLUMN resource_mode')

def remove_v16(db):
    for table in ('context_reservations','context_budget_tasks'):
        present=db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone()
        if present:
            assert db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() is None
            db.execute('DROP TABLE '+table)
    if 'meter_kind' in {r[1] for r in db.execute('PRAGMA table_info(provider_guard_calls)')}:
        db.execute('ALTER TABLE provider_guard_calls DROP COLUMN meter_kind')
    remove_v18(db)
    for table in ('product_account_events','product_account_state'):
        db.execute(f'DROP TABLE IF EXISTS {table}')
    for table in ('feedback_evidence','feedback_cases','conversation_clarifications','intent_semantic_examples',
        'intent_semantic_policy','storage_reservations','document_storage_owners'):
        db.execute(f'DROP TABLE IF EXISTS {table}')
    for table,columns in {'parse_tasks':['owner_user_id'],'provider_circuits':['generation'],
        'provider_guard_calls':['usage_json','delivery_status'],'kb_task_calls':['delivery_status'],
        'question_calls':['delivery_status'],'analysis_calls':['delivery_status'],
        'answer_feedback':['generation','fix_revision','client_reply']}.items():
        present={r[1] for r in db.execute(f'PRAGMA table_info({table})')}
        for name in columns:
            if name in present:db.execute(f'ALTER TABLE {table} DROP COLUMN {name}')
