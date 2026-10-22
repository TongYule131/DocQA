"""统一知识库入口的增量结构；旧问答、原件、解析、向量和调用账本不改写。"""
import json

UNIFIED_SCHEMA = """
ALTER TABLE knowledge_bases ADD COLUMN kind TEXT NOT NULL DEFAULT 'team' CHECK(kind IN ('team','temporary','legacy'));
ALTER TABLE knowledge_bases ADD COLUMN activation_policy TEXT NOT NULL DEFAULT 'auto' CHECK(activation_policy IN ('auto','review'));
ALTER TABLE knowledge_bases ADD COLUMN expires_at TEXT;
ALTER TABLE knowledge_bases ADD COLUMN revoked_at TEXT;
ALTER TABLE knowledge_bases ADD COLUMN purged_at TEXT;
ALTER TABLE kb_conversations ADD COLUMN document_filter_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE kb_conversations ADD COLUMN memory_notes TEXT NOT NULL DEFAULT '';
ALTER TABLE kb_conversations ADD COLUMN initial_context_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE kb_conversations ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE kb_conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1));
ALTER TABLE kb_conversations ADD COLUMN deleted_at TEXT;
CREATE TABLE legacy_document_bases (
 document_id TEXT PRIMARY KEY REFERENCES documents(id), base_id TEXT NOT NULL REFERENCES knowledge_bases(id)
);
CREATE TABLE unified_chat_links (
 source TEXT NOT NULL CHECK(source IN ('document','single')), source_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL UNIQUE REFERENCES kb_conversations(id), PRIMARY KEY(source,source_id)
);
CREATE TABLE temporary_base_keys (
 owner_key TEXT NOT NULL, request_key TEXT NOT NULL, base_id TEXT NOT NULL UNIQUE REFERENCES knowledge_bases(id),
 PRIMARY KEY(owner_key,request_key)
);
CREATE TABLE ingestion_jobs (
 id TEXT PRIMARY KEY, base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
 document_id TEXT NOT NULL UNIQUE REFERENCES documents(id), owner_user_id TEXT REFERENCES product_users(id),
 source_hash TEXT NOT NULL, configuration TEXT NOT NULL, replacement_key TEXT NOT NULL DEFAULT '',
 replace_document_id TEXT REFERENCES documents(id), status TEXT NOT NULL DEFAULT 'queued'
 CHECK(status IN ('queued','running','succeeded','failed','needs_attention','cancelled','review')),
 stage TEXT NOT NULL DEFAULT 'uploaded', parse_task_id TEXT REFERENCES parse_tasks(id),
 parse_version_id TEXT REFERENCES parse_versions(id), index_task_id TEXT REFERENCES kb_tasks(id),
 index_id TEXT REFERENCES embedding_indexes(id), retry_count INTEGER NOT NULL DEFAULT 0,
 error_code TEXT, error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(base_id,source_hash,configuration,replacement_key)
);
CREATE INDEX ix_ingestion_status ON ingestion_jobs(status,created_at);
CREATE TABLE ingestion_request_keys (
 base_id TEXT NOT NULL REFERENCES knowledge_bases(id), request_key TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES ingestion_jobs(id), fingerprint TEXT NOT NULL,
 PRIMARY KEY(base_id,request_key)
);
CREATE TABLE chat_submissions (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES kb_conversations(id),
 owner_user_id TEXT REFERENCES product_users(id), request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 question TEXT NOT NULL, document_ids_json TEXT NOT NULL, waiting_jobs_json TEXT NOT NULL,
 memory_epoch INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'waiting'
 CHECK(status IN ('waiting','submitted','failed','cancelled')),
 task_id TEXT UNIQUE REFERENCES kb_tasks(id), error_code TEXT, error_message TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(conversation_id,request_key)
);
CREATE UNIQUE INDEX ux_chat_submission_waiting ON chat_submissions(conversation_id) WHERE status='waiting';
"""


def migrate_legacy_scopes(db):
    """只增加私有资料范围投影，不复制回答，也不把旧资料发布到团队知识库。"""
    from app.repository import now_iso
    stamp=now_iso(); legacy_id='kb-legacy-private'
    docs=db.execute('SELECT id FROM documents ORDER BY id').fetchall()
    for (document_id,) in docs:
        existing=db.execute('SELECT knowledge_base_id FROM knowledge_base_documents WHERE document_id=?',(document_id,)).fetchone()
        if existing:
            base_id=existing[0]
        else:
            attachment=db.execute('SELECT owner_user_id,expires_at,revoked_at FROM conversation_attachments WHERE document_id=?',(document_id,)).fetchone()
            if attachment:
                owner,expires,revoked=tuple(attachment);base_id='kb-legacy-temp-'+document_id
                db.execute("INSERT INTO knowledge_bases(id,name,description,created_by,created_at,updated_at,collection_name,kind,expires_at,revoked_at) VALUES (?,?,?,?,?,?,?,'temporary',?,?)",
                    (base_id,'历史会话资料','由原附件范围迁移',owner,stamp,stamp,base_id.replace('-',''),expires,revoked))
            else:
                base_id=legacy_id
                if not db.execute('SELECT 1 FROM knowledge_bases WHERE id=?',(base_id,)).fetchone():
                    collection='legacyprivate';suffix=0
                    while db.execute('SELECT 1 FROM knowledge_bases WHERE collection_name=?',(collection,)).fetchone():
                        suffix+=1;collection='legacyprivate'+str(suffix)
                    db.execute("INSERT INTO knowledge_bases(id,name,description,created_at,updated_at,collection_name,kind) VALUES (?,?,?,?,?,?,'legacy')",
                        (base_id,'历史资料（管理员私有）','保留旧资料和来源，未共享给客户',stamp,stamp,collection))
            db.execute('INSERT INTO knowledge_base_documents VALUES (?,?,?)',(base_id,document_id,stamp))
        db.execute('INSERT INTO legacy_document_bases VALUES (?,?)',(document_id,base_id))
    # 仅为新建私有库投影原本已经成功的活动索引；读取时仍重验真实来源与模型签名。
    for (base_id,) in db.execute("SELECT id FROM knowledge_bases WHERE kind IN ('legacy','temporary') AND active_snapshot_id IS NULL").fetchall():
        ready=db.execute("SELECT d.id,v.id,i.id,v.origin_hash,i.source_signature FROM knowledge_base_documents b JOIN documents d ON d.id=b.document_id JOIN parse_versions v ON v.id=d.active_parse_version_id JOIN embedding_indexes i ON i.id=d.active_index_id WHERE b.knowledge_base_id=? AND i.parse_version_id=v.id AND i.status='indexed' AND v.quality_status!='invalid'",(base_id,)).fetchall()
        if not ready:continue
        snapshot='kbs-migrated-'+base_id
        db.execute('INSERT INTO knowledge_base_snapshots VALUES (?,?,1,NULL,?)',(snapshot,base_id,stamp))
        for item in ready:db.execute('INSERT INTO knowledge_base_snapshot_documents VALUES (?,?,?,?,?,?)',(snapshot,*tuple(item)))
        db.execute('UPDATE knowledge_bases SET active_snapshot_id=?,revision=1 WHERE id=?',(snapshot,base_id))
