"""统一上下文计量：只新增元数据表，旧请求与业务行不回填。"""

def migrate(db):
    db.execute("ALTER TABLE provider_guard_calls ADD COLUMN meter_kind TEXT CHECK(meter_kind IN ('deepseek','embedding','tool_http'))")
    db.execute('''CREATE TABLE context_budget_tasks (
        task_key TEXT PRIMARY KEY, owner_key TEXT NOT NULL, policy_hash TEXT NOT NULL,
        task_limit INTEGER NOT NULL CHECK(task_limit>0), created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE context_reservations (
        call_id TEXT PRIMARY KEY REFERENCES provider_guard_calls(id),
        task_key TEXT NOT NULL REFERENCES context_budget_tasks(task_key), owner_key TEXT NOT NULL,
        day TEXT NOT NULL, provider TEXT NOT NULL, profile TEXT NOT NULL,
        input_units INTEGER NOT NULL, output_reserved INTEGER NOT NULL,
        reserved_units INTEGER NOT NULL, charged_units INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','reported','unknown','uncertain','not_sent','overrun')),
        request_hash TEXT NOT NULL, metadata_json TEXT NOT NULL, usage_json TEXT,
        created_at TEXT NOT NULL, settled_at TEXT)''')
    db.execute('CREATE INDEX ix_context_owner_day ON context_reservations(owner_key,day)')
    db.execute('CREATE INDEX ix_context_task ON context_reservations(task_key)')
    db.execute('CREATE INDEX ix_context_state ON context_reservations(state)')
