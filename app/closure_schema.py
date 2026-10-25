"""完整性复核后的增量结构；旧账本及原有调用状态保持。"""


def migrate(db):
    db.execute('ALTER TABLE parse_tasks ADD COLUMN owner_user_id TEXT REFERENCES product_users(id)')
    db.execute('ALTER TABLE provider_circuits ADD COLUMN generation INTEGER NOT NULL DEFAULT 0')
    db.execute('ALTER TABLE provider_guard_calls ADD COLUMN usage_json TEXT')
    for table in ('provider_guard_calls', 'kb_task_calls', 'question_calls', 'analysis_calls'):
        db.execute(f"ALTER TABLE {table} ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'legacy_unknown'")
    db.execute('''CREATE TABLE intent_semantic_examples (
        node_id TEXT NOT NULL REFERENCES intent_nodes(id) ON DELETE CASCADE, revision INTEGER NOT NULL,
        example_hash TEXT NOT NULL, provider_signature TEXT NOT NULL, vector_json TEXT NOT NULL,
        PRIMARY KEY(node_id,revision,example_hash,provider_signature))''')
    db.execute('''CREATE TABLE intent_semantic_policy (
        id INTEGER PRIMARY KEY CHECK(id=1), threshold REAL NOT NULL, margin REAL NOT NULL,
        dataset_hash TEXT NOT NULL, provider_signature TEXT NOT NULL, report_json TEXT NOT NULL,
        configuration TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE conversation_clarifications (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES kb_tasks(id),
        conversation_id TEXT NOT NULL, owner_user_id TEXT, epoch INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'awaiting', needed_count INTEGER NOT NULL,
        choices_json TEXT NOT NULL, resolve_key TEXT, resolve_hash TEXT, resolved_submission_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    for column in ("generation INTEGER NOT NULL DEFAULT 1", "fix_revision TEXT", "client_reply TEXT"):
        db.execute('ALTER TABLE answer_feedback ADD COLUMN '+column)
    db.execute('''CREATE TABLE feedback_cases (
        id TEXT PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES answer_feedback(id), generation INTEGER NOT NULL,
        case_sha256 TEXT NOT NULL, payload_json TEXT, created_at TEXT NOT NULL,
        UNIQUE(feedback_id,generation))''')
    db.execute('''CREATE TABLE feedback_evidence (
        id TEXT PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES answer_feedback(id), generation INTEGER NOT NULL,
        case_id TEXT NOT NULL REFERENCES feedback_cases(id), kind TEXT NOT NULL CHECK(kind IN ('original','negative')),
        code_identity TEXT NOT NULL, fixture_sha256 TEXT NOT NULL, report_sha256 TEXT NOT NULL,
        test_name TEXT NOT NULL, run_id TEXT NOT NULL, summary_json TEXT NOT NULL, reviewer_id TEXT, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE document_storage_owners (
        document_id TEXT PRIMARY KEY REFERENCES documents(id), owner_key TEXT NOT NULL, byte_size INTEGER NOT NULL)''')
    db.execute('''INSERT INTO document_storage_owners SELECT d.id,
        COALESCE((SELECT i.owner_user_id FROM ingestion_jobs i WHERE i.document_id=d.id LIMIT 1),'local'),d.size
        FROM documents d WHERE d.filename!='已清理临时资料' ''')
    db.execute('''CREATE TABLE storage_reservations (
        document_id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, byte_size INTEGER NOT NULL, created_at TEXT NOT NULL)''')
