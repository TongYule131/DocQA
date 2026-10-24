"""S3～S5 的持久状态；不改写历史回答、引用或调用账本。"""
TABLES = """
CREATE TABLE conversation_entity_memory (
 conversation_id TEXT NOT NULL, epoch INTEGER NOT NULL,
 version TEXT NOT NULL, entities_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT NOT NULL, PRIMARY KEY(conversation_id,epoch)
);
CREATE TABLE answer_feedback (
 id TEXT PRIMARY KEY, owner_user_id TEXT REFERENCES product_users(id),
 source TEXT NOT NULL CHECK(source IN ('knowledge','question')), task_id TEXT NOT NULL,
 request_key TEXT NOT NULL, category TEXT NOT NULL, comment TEXT NOT NULL,
 share_context INTEGER NOT NULL DEFAULT 0, context_json TEXT,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','reproduced','fixed','verified','closed')),
 root_cause TEXT, regression_case TEXT, original_retest TEXT, negative_retest TEXT,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_feedback_request ON answer_feedback(COALESCE(owner_user_id,''),request_key);
CREATE INDEX ix_feedback_status ON answer_feedback(status,created_at);
CREATE TABLE feedback_events (
 id TEXT PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES answer_feedback(id),
 actor_id TEXT REFERENCES product_users(id), status TEXT NOT NULL, detail_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE provider_guard_calls (
 id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, provider_key TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, deadline TEXT NOT NULL
);
CREATE INDEX ix_guard_calls_day ON provider_guard_calls(started_at,owner_key);
CREATE TABLE provider_circuits (
 provider_key TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
 open_until TEXT, probe_id TEXT, probe_until TEXT
);
CREATE TABLE dispatch_fairness (
 owner_key TEXT PRIMARY KEY, last_started_at TEXT NOT NULL
);
"""


def migrate(db):
    # executescript 会隐式提交，迁移必须逐条执行以保留整轮原子性。
    for statement in TABLES.split(';'):
        if statement.strip():
            db.execute(statement)
