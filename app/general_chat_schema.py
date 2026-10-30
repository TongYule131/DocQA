"""普通会话与明确资料范围；不重写旧范围或历史问答。"""


def migrate(db):
    # 在迁移器的单一事务和外键关闭区间重建，子表仍引用原表名。
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='kb_conversations'").fetchone()[0]
    if 'base_id TEXT NOT NULL REFERENCES knowledge_bases(id)' not in sql:
        raise ValueError('unexpected_conversation_schema')
    indexes = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='kb_conversations' AND sql IS NOT NULL")]
    columns = [row[1] for row in db.execute('PRAGMA table_info(kb_conversations)')]
    sql = sql.replace('CREATE TABLE kb_conversations', 'CREATE TABLE kb_conversations_v18', 1)
    sql = sql.replace('base_id TEXT NOT NULL REFERENCES knowledge_bases(id)', 'base_id TEXT REFERENCES knowledge_bases(id)', 1)
    db.execute(sql)
    names = ','.join('"' + name + '"' for name in columns)
    db.execute(f'INSERT INTO kb_conversations_v18 ({names}) SELECT {names} FROM kb_conversations')
    db.execute('DROP TABLE kb_conversations')
    db.execute('ALTER TABLE kb_conversations_v18 RENAME TO kb_conversations')
    for statement in indexes:
        db.execute(statement)
    for table in ('kb_conversations', 'chat_submissions'):
        db.execute(f"ALTER TABLE {table} ADD COLUMN resource_mode TEXT CHECK(resource_mode IN ('none','selected','all'))")
