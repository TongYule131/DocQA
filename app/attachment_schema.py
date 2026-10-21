"""临时资料与会话一对一绑定；保留到期墓碑，防止请求键重新创建资料。"""

ATTACHMENT_TABLES = """
CREATE TABLE conversation_attachments (
    document_id TEXT PRIMARY KEY REFERENCES documents(id),
    conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
    owner_user_id TEXT REFERENCES product_users(id),
    expires_at TEXT NOT NULL, revoked_at TEXT, purged_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_attachments_expiry ON conversation_attachments(expires_at,purged_at);
"""


def attachment_available(db, document_id, *, conversation_id=None):
    """供worker在短事务内复查；普通文档不受附件策略影响。"""
    from app.repository import now_iso
    row = db.execute('SELECT a.*,c.deleted_at FROM conversation_attachments a '
                     'JOIN conversations c ON c.id=a.conversation_id WHERE a.document_id=?', (document_id,)).fetchone()
    if not row:
        return True
    return bool(not row['revoked_at'] and not row['purged_at'] and not row['deleted_at']
                and row['expires_at'] > now_iso()
                and (conversation_id is None or row['conversation_id'] == conversation_id))
