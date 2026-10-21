"""知识库处理、库级问答、意图配置与安全链路元数据。"""
KNOWLEDGE_TABLES = """
ALTER TABLE knowledge_bases ADD COLUMN collection_name TEXT;
ALTER TABLE knowledge_bases ADD COLUMN embedding_signature TEXT;
ALTER TABLE knowledge_bases ADD COLUMN embedding_model TEXT;
ALTER TABLE knowledge_bases ADD COLUMN chunk_max_chars INTEGER NOT NULL DEFAULT 800;
ALTER TABLE knowledge_bases ADD COLUMN chunk_overlap_chars INTEGER NOT NULL DEFAULT 120;
ALTER TABLE knowledge_bases ADD COLUMN chunk_strategy TEXT NOT NULL DEFAULT 'structure_aware';
UPDATE knowledge_bases SET collection_name=replace(id,'-','') WHERE collection_name IS NULL;
CREATE UNIQUE INDEX ux_kb_collection ON knowledge_bases(collection_name);
CREATE TABLE kb_tasks (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('index','question')),
 base_id TEXT REFERENCES knowledge_bases(id), document_id TEXT REFERENCES documents(id),
 owner_user_id TEXT REFERENCES product_users(id), request_key TEXT NOT NULL,
 input_json TEXT NOT NULL, input_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','needs_attention','cancelled')),
 stage TEXT NOT NULL DEFAULT 'queued', error_code TEXT, error_message TEXT,
 max_requests INTEGER NOT NULL, requests_used INTEGER NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_expires_at TEXT,
 attempt_count INTEGER NOT NULL DEFAULT 0, result_json TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE UNIQUE INDEX ux_kb_task_request ON kb_tasks(COALESCE(owner_user_id,''),kind,request_key);
CREATE UNIQUE INDEX ux_kb_index_active ON kb_tasks(document_id) WHERE kind='index' AND status IN ('queued','running');
CREATE INDEX ix_kb_task_status ON kb_tasks(status,created_at);
CREATE TABLE kb_task_calls (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES kb_tasks(id), sequence_no INTEGER NOT NULL,
 role TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT, usage_json TEXT,
 started_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER, UNIQUE(task_id,sequence_no)
);
CREATE TABLE kb_task_checkpoints (
 task_id TEXT NOT NULL REFERENCES kb_tasks(id), name TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(task_id,name)
);
CREATE TABLE kb_trace_spans (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES kb_tasks(id), attempt_no INTEGER NOT NULL,
 parent_id TEXT, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER, error_code TEXT
);
CREATE INDEX ix_kb_trace_task ON kb_trace_spans(task_id,started_at);
CREATE TABLE kb_chunk_revisions (
 version_id TEXT PRIMARY KEY REFERENCES parse_versions(id), parent_version_id TEXT NOT NULL REFERENCES parse_versions(id),
 document_id TEXT NOT NULL REFERENCES documents(id), actor_id TEXT REFERENCES product_users(id),
 reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE intent_nodes (
 id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 parent_id TEXT REFERENCES intent_nodes(id), kind TEXT NOT NULL CHECK(kind IN ('KB','SYSTEM','MCP')),
 base_id TEXT REFERENCES knowledge_bases(id), tool_id TEXT,
 description TEXT NOT NULL DEFAULT '', examples_json TEXT NOT NULL DEFAULT '[]', response_text TEXT NOT NULL DEFAULT '',
 top_k INTEGER, sort_order INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE kb_conversations (
 id TEXT PRIMARY KEY, base_id TEXT NOT NULL REFERENCES knowledge_bases(id), owner_user_id TEXT REFERENCES product_users(id),
 title TEXT NOT NULL, request_key TEXT NOT NULL, memory_enabled INTEGER NOT NULL DEFAULT 1,
 memory_epoch INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_kb_conversation_request ON kb_conversations(COALESCE(owner_user_id,''),request_key);
CREATE TABLE kb_conversation_turns (
 conversation_id TEXT NOT NULL REFERENCES kb_conversations(id), turn_no INTEGER NOT NULL,
 task_id TEXT NOT NULL UNIQUE REFERENCES kb_tasks(id), PRIMARY KEY(conversation_id,turn_no)
);
CREATE TABLE mcp_tools (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, endpoint TEXT NOT NULL, tool_name TEXT NOT NULL,
 auth_env TEXT, input_schema_json TEXT NOT NULL, protocol_version TEXT NOT NULL DEFAULT '2025-11-25',
 enabled INTEGER NOT NULL DEFAULT 0, read_only INTEGER NOT NULL DEFAULT 1,
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def seed_intents(db):
    """初始系统交互不包含虚构的业务数据或工具连接。"""
    from app.repository import now_iso
    import json
    rows = [
        ('sys','系统交互',None,[],''),
        ('sys-welcome','欢迎与问候','intent-sys',['你好','您好','hello','hi','在吗','早上好'],'你好，我是 DocQA。可以帮你查阅获授权的知识库资料。'),
        ('sys-about','关于助手','intent-sys',['你是谁','你能做什么','你是ChatGPT吗'],'我是 DocQA 文档知识助手，依据你获授权的资料回答问题，并提供原文来源。'),
        ('sys-thanks','感谢与表扬','intent-sys',['谢谢','谢谢你','干得不错','很有帮助','太棒了'],'谢谢你的反馈，很高兴能帮到你。'),
        ('sys-correction','质疑与纠正','intent-sys',['你说的不对','不太准确','答非所问'],'收到。请指出不准确的部分，我会结合原文重新核对。'),
    ]
    for order,(code,name,parent,examples,response) in enumerate(rows):
        stamp=now_iso()
        db.execute('INSERT OR IGNORE INTO intent_nodes(id,code,name,parent_id,kind,examples_json,response_text,sort_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   ('intent-'+code,code,name,parent,'SYSTEM',json.dumps(examples,ensure_ascii=False),response,order,stamp,stamp))
