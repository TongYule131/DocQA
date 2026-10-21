"""产品化身份与发布快照；与主库同事务迁移，不另建绕过版本检查的侧库。"""

PRODUCT_TABLES = """
CREATE TABLE product_users (
    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','customer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL
);
CREATE TABLE product_sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES product_users(id),
    csrf_token TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX ix_product_sessions_user ON product_sessions(user_id);
CREATE TABLE product_login_limits (
    key TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start INTEGER NOT NULL
);
CREATE TABLE knowledge_bases (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    created_by TEXT REFERENCES product_users(id), created_at TEXT NOT NULL,
    active_snapshot_id TEXT, revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE knowledge_base_documents (
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
    document_id TEXT PRIMARY KEY REFERENCES documents(id), added_at TEXT NOT NULL
);
CREATE TABLE knowledge_base_members (
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
    user_id TEXT NOT NULL REFERENCES product_users(id), created_at TEXT NOT NULL,
    PRIMARY KEY(knowledge_base_id,user_id)
);
CREATE TABLE knowledge_base_snapshots (
    id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
    revision INTEGER NOT NULL, created_by TEXT REFERENCES product_users(id), created_at TEXT NOT NULL,
    UNIQUE(knowledge_base_id,revision)
);
CREATE TABLE knowledge_base_snapshot_documents (
    snapshot_id TEXT NOT NULL REFERENCES knowledge_base_snapshots(id),
    document_id TEXT NOT NULL REFERENCES documents(id),
    parse_version_id TEXT NOT NULL REFERENCES parse_versions(id),
    index_id TEXT NOT NULL REFERENCES embedding_indexes(id),
    origin_hash TEXT NOT NULL, source_signature TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,document_id)
);
CREATE TABLE document_upload_requests (
    scope TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents(id), created_at TEXT NOT NULL,
    PRIMARY KEY(scope,request_key)
);
ALTER TABLE question_jobs ADD COLUMN owner_user_id TEXT REFERENCES product_users(id);
ALTER TABLE question_jobs ADD COLUMN knowledge_snapshot_id TEXT REFERENCES knowledge_base_snapshots(id);
CREATE INDEX ix_question_jobs_owner ON question_jobs(owner_user_id,created_at);
"""
