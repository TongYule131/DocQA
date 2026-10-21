"""会话与轮次只关联原问答任务，不复制答案、调用账本或另建队列。"""

CONVERSATION_TABLES = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY, owner_user_id TEXT REFERENCES product_users(id),
    document_id TEXT NOT NULL REFERENCES documents(id), title TEXT NOT NULL,
    request_key TEXT NOT NULL, memory_enabled INTEGER NOT NULL DEFAULT 1 CHECK(memory_enabled IN (0,1)),
    memory_notes TEXT NOT NULL DEFAULT '', memory_epoch INTEGER NOT NULL DEFAULT 0,
    memory_after_turn INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)), deleted_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_conversations_request ON conversations(COALESCE(owner_user_id,''),request_key);
CREATE INDEX ix_conversations_owner ON conversations(owner_user_id,updated_at);
ALTER TABLE question_jobs ADD COLUMN conversation_id TEXT REFERENCES conversations(id);
CREATE TABLE conversation_turns (
    conversation_id TEXT NOT NULL REFERENCES conversations(id), turn_no INTEGER NOT NULL,
    request_key TEXT NOT NULL, question TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE REFERENCES question_jobs(id),
    memory_epoch INTEGER NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(conversation_id,turn_no), UNIQUE(conversation_id,request_key)
);
CREATE INDEX ix_question_jobs_conversation ON question_jobs(conversation_id,status);
"""
