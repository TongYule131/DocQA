"""旧版本夹具撤销v16增量，禁止只改迁移登记伪造历史schema。"""
def remove_v16(db):
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
