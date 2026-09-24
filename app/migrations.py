# 数据库迁移：带版本号的 SQLite 增量迁移，同时支持空库与既有旧库。
#
# 设计要点：
# 1. 迁移是有版本号的，重复运行不会重复改写（schema_migrations 记录已应用版本）。
# 2. 迁移前用 SQLite 一致性备份（sqlite3.Connection.backup）生成副本，兼容 WAL。
# 3. 单个迁移版本在事务中执行；失败即回滚并抛出 MigrationError，由调用方停止启动，
#    绝不静默新建空数据库掩盖失败。
# 4. 旧库的文档、分块 ID、向量、维度、提供方签名和索引状态全部保留；旧内容建立
#    legacy 解析版本，不重新 OCR、不调用 embedding。
import logging
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 当前代码期望的数据库结构版本；新增迁移时递增。
SCHEMA_VERSION = 2

# 旧 source_signature 算法标识：sha256(json(chunk.model_dump()))，保留用于验证旧索引。
SIGNATURE_ALGO_LEGACY = "sha256-chunk-dump-v1"
# 新算法标识：绑定解析版本、块 ID、顺序、类型与文本。
SIGNATURE_ALGO_VERSIONED = "sha256-version-chunks-v2"


class MigrationError(Exception):
    """迁移失败：调用方必须停止启动并保留原库与备份。"""


# ---------------------------------------------------------------------------
# v1：解析版本、持久化任务、来源、质量告警与版本化索引
# ---------------------------------------------------------------------------
# 说明：documents 表沿用旧表名并新增列，以保留旧文档 ID 与既有数据。
_MIGRATION_V1_DOCUMENT_COLUMNS = """
ALTER TABLE documents ADD COLUMN format TEXT;
ALTER TABLE documents ADD COLUMN page TEXT;
ALTER TABLE documents ADD COLUMN active_parse_version_id TEXT REFERENCES parse_versions(id);
-- active_index_id 只作为指针保存：embedding_indexes 的主键是 document_id，
-- 若在此声明外键会造成 "foreign key mismatch"，因此该列不声明外键约束。
ALTER TABLE documents ADD COLUMN active_index_id TEXT;
"""

_MIGRATION_V1_TABLES = """
-- 解析任务：SQLite 是任务事实来源。partial unique index 保证同一文档最多一个活动任务。
CREATE TABLE IF NOT EXISTS parse_tasks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'queued',
    idempotency_key TEXT,
    request_fingerprint TEXT,
    request_summary TEXT,
    upstream_task_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    lease_token TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    CHECK (status IN ('queued','running','succeeded','failed','needs_attention'))
);
CREATE INDEX IF NOT EXISTS ix_parse_tasks_document ON parse_tasks(document_id, created_at);
-- 同一文档最多一个 queued/running 任务；needs_attention 需要用户显式处理，不占用活动位。
CREATE UNIQUE INDEX IF NOT EXISTS ux_parse_tasks_active
    ON parse_tasks(document_id) WHERE status IN ('queued','running');
-- 幂等键在同一文档内唯一；不同请求体使用同一键时由业务层返回 409。
CREATE UNIQUE INDEX IF NOT EXISTS ux_parse_tasks_idempotency
    ON parse_tasks(document_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- 任务尝试记录：每次真实执行（含恢复）留痕，便于区分重试与新建任务。
CREATE TABLE IF NOT EXISTS parse_attempts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES parse_tasks(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    lease_token TEXT,
    upstream_task_id TEXT,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_parse_attempts_task ON parse_attempts(task_id, attempt_no);

-- 不可变解析版本：一次成功解析的完整产物引用与质量状态。
CREATE TABLE IF NOT EXISTS parse_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES parse_tasks(id),
    origin_hash TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT,
    config_summary TEXT NOT NULL,
    result_schema_version TEXT NOT NULL,
    result_hash TEXT,
    quality_status TEXT NOT NULL,
    quality_summary TEXT,
    block_count INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    result_json_path TEXT,
    markdown_path TEXT,
    created_at TEXT NOT NULL,
    CHECK (quality_status IN ('ok','warnings','invalid'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_parse_versions_result
    ON parse_versions(document_id, result_hash) WHERE result_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_parse_versions_document ON parse_versions(document_id, created_at);

-- 结构化块：按 Docling 阅读顺序保存，供预览与分块使用。
CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parse_version_id TEXT NOT NULL REFERENCES parse_versions(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    block_type TEXT NOT NULL,
    label TEXT,
    text TEXT NOT NULL DEFAULT '',
    heading_path TEXT,
    table_json TEXT,
    node_ref TEXT,
    sheet_name TEXT,
    char_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (parse_version_id, order_index)
);
CREATE INDEX IF NOT EXISTS ix_blocks_version ON blocks(parse_version_id, order_index);

-- 来源记录：一个块可对应多个来源，格式和单位必须显式保存。
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parse_version_id TEXT NOT NULL REFERENCES parse_versions(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL DEFAULT 0,
    format TEXT NOT NULL,
    page_no INTEGER,
    page_end INTEGER,
    bbox_json TEXT,
    coord_origin TEXT,
    coord_unit TEXT,
    section_path TEXT,
    table_no INTEGER,
    row_index INTEGER,
    col_index INTEGER,
    row_span INTEGER,
    col_span INTEGER,
    sheet_name TEXT,
    cell_range TEXT,
    line_start INTEGER,
    line_end INTEGER,
    node_ref TEXT,
    char_start INTEGER,
    char_end INTEGER,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_sources_block ON sources(block_id, ordinal);
CREATE INDEX IF NOT EXISTS ix_sources_version ON sources(parse_version_id);

-- 质量告警：稳定告警码 + 中文说明 + 严重程度 + 受影响来源。
CREATE TABLE IF NOT EXISTS quality_warnings (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parse_version_id TEXT NOT NULL REFERENCES parse_versions(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL,
    scope TEXT NOT NULL,
    block_id TEXT REFERENCES blocks(id) ON DELETE CASCADE,
    source_json TEXT,
    page_no INTEGER,
    sheet_name TEXT,
    detail TEXT,
    created_at TEXT NOT NULL,
    CHECK (severity IN ('info','warning','error'))
);
CREATE INDEX IF NOT EXISTS ix_warnings_version ON quality_warnings(parse_version_id, severity);

-- 索引构建尝试：每次候选索引构建单独记录，成功与失败分别留痕。
CREATE TABLE IF NOT EXISTS index_attempts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    index_id TEXT REFERENCES embedding_indexes(id) ON DELETE CASCADE,
    target_parse_version_id TEXT NOT NULL,
    provider_signature TEXT,
    source_signature TEXT,
    source_signature_algo TEXT,
    dimension INTEGER,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT,
    CHECK (status IN ('pending','indexing','indexed','failed','superseded'))
);
CREATE INDEX IF NOT EXISTS ix_index_attempts_document ON index_attempts(document_id, started_at);

-- 向量：绑定唯一索引与唯一分块；chunk_id 唯一约束保证一个分块在库中只有一个向量。
-- 注意：该表在重建 chunks 表之后创建（见 _MIGRATION_V1_REBUILD_CHUNKS_TABLE），
-- 以保证外键指向新的 chunks 结构。
"""

# 重建索引表：旧表以 document_id 为主键，无法承载“索引 ID + 绑定解析版本”。
# 迁移时先把旧数据复制到新结构，再替换旧表，最后重建外键引用它的表。
_MIGRATION_V1_REBUILD_INDEX_TABLE = """
CREATE TABLE embedding_indexes_new (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    provider_signature TEXT,
    source_signature TEXT,
    source_signature_algo TEXT,
    dimension INTEGER,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    parse_version_id TEXT REFERENCES parse_versions(id),
    model_signature TEXT,
    created_at TEXT,
    activated_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 0,
    chunking_algo TEXT,
    UNIQUE (document_id)
);
INSERT INTO embedding_indexes_new
    (id, document_id, status, provider_signature, source_signature, dimension, chunk_count, error)
SELECT COALESCE(id, 'legacy-index-' || document_id), document_id, status, provider_signature,
       source_signature, dimension, chunk_count, error
FROM embedding_indexes;
DROP TABLE embedding_indexes;
ALTER TABLE embedding_indexes_new RENAME TO embedding_indexes;
CREATE UNIQUE INDEX IF NOT EXISTS ux_embedding_indexes_id ON embedding_indexes(id);
CREATE INDEX IF NOT EXISTS ix_embedding_indexes_document ON embedding_indexes(document_id);
"""

# 旧表 embedding_indexes 扩展为“解析版本绑定的索引”，并保留旧列用于兼容读取。
_MIGRATION_V1_INDEX_COLUMNS = """
ALTER TABLE embedding_indexes ADD COLUMN id TEXT;
ALTER TABLE embedding_indexes ADD COLUMN parse_version_id TEXT REFERENCES parse_versions(id);
ALTER TABLE embedding_indexes ADD COLUMN model_signature TEXT;
ALTER TABLE embedding_indexes ADD COLUMN source_signature_algo TEXT;
ALTER TABLE embedding_indexes ADD COLUMN created_at TEXT;
ALTER TABLE embedding_indexes ADD COLUMN activated_at TEXT;
ALTER TABLE embedding_indexes ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0;
ALTER TABLE embedding_indexes ADD COLUMN chunking_algo TEXT;
-- 向量表以 index_id 外键引用索引；SQLite 要求被引用列有唯一约束。
-- 旧表主键是 document_id，这里为新增的 id 列补唯一索引，使外键可解析。
CREATE UNIQUE INDEX IF NOT EXISTS ux_embedding_indexes_id ON embedding_indexes(id);
"""

# 旧 chunks 表扩展：解析版本归属、显式顺序、块类型与块关联；page 允许为空（DOCX/XLSX 无真实页码）。
_MIGRATION_V1_CHUNK_COLUMNS = """
ALTER TABLE chunks ADD COLUMN parse_version_id TEXT REFERENCES parse_versions(id);
ALTER TABLE chunks ADD COLUMN block_id TEXT REFERENCES blocks(id);
ALTER TABLE chunks ADD COLUMN order_index INTEGER;
ALTER TABLE chunks ADD COLUMN chunk_type TEXT;
ALTER TABLE chunks ADD COLUMN sources_json TEXT;
ALTER TABLE chunks ADD COLUMN char_count INTEGER;
"""

_MIGRATION_V1_CHUNK_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_chunks_version ON chunks(parse_version_id, order_index);
CREATE UNIQUE INDEX IF NOT EXISTS ux_chunks_version_order
    ON chunks(parse_version_id, order_index) WHERE parse_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_chunks_document_order ON chunks(document_id, order_index);
"""

# 重建分块表：旧表的 page 是 NOT NULL，而 DOCX/XLSX 没有真实页码，必须允许为空。
# 与索引表一样，先把旧数据复制到新结构，再替换旧表，最后重建外键引用它的向量表。
_MIGRATION_V1_REBUILD_CHUNKS_TABLE = """
CREATE TABLE chunks_new (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER,
    text TEXT NOT NULL,
    parse_version_id TEXT REFERENCES parse_versions(id),
    block_id TEXT,
    order_index INTEGER,
    chunk_type TEXT,
    sources_json TEXT,
    char_count INTEGER
);
INSERT INTO chunks_new (id, document_id, page, text, parse_version_id, block_id, order_index,
                        chunk_type, sources_json, char_count)
SELECT id, document_id, page, text, parse_version_id, block_id, order_index,
       COALESCE(chunk_type, 'legacy'), sources_json, char_count
FROM chunks;
DROP TABLE chunks;
ALTER TABLE chunks_new RENAME TO chunks;
CREATE TABLE embedding_vectors_v2 (
    index_id TEXT NOT NULL REFERENCES embedding_indexes(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    vector TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    PRIMARY KEY (index_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS ix_vectors_v2_index ON embedding_vectors_v2(index_id);
CREATE INDEX IF NOT EXISTS ix_vectors_v2_document ON embedding_vectors_v2(document_id);
CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);
"""


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def detect_version(db: sqlite3.Connection) -> int:
    """返回当前数据库结构版本。

    兼容旧库：旧实现没有 schema_migrations 表，但可能已经建好 documents/chunks 等表，
    因此以“是否存在 documents 表”区分全新空库（0）与旧库（0，但已有数据）。
    """
    if not _table_exists(db, "schema_migrations"):
        return 0
    row = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def backup_database(path: Path, backup_dir: Path) -> Path | None:
    """在迁移前生成一致性备份。

    直接复制运行中的 SQLite 主文件可能得到损坏或不含 WAL 内容的副本；这里使用
    sqlite3 的在线备份 API，在事务一致点读取整个数据库（含 WAL 中已提交数据）。
    """
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{path.stem}-{stamp}.db"
    counter = 1
    while target.exists():
        target = backup_dir / f"{path.stem}-{stamp}-{counter}.db"
        counter += 1
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
    logger.info("数据库迁移备份：%s", target)
    return target


def _apply_v1(db: sqlite3.Connection) -> None:
    """v1 迁移：新增契约表，扩展旧表，并把旧内容登记为 legacy 解析版本。"""
    now = datetime.now(timezone.utc).isoformat()
    # 旧表必须先扩展，_MIGRATION_V1 中的 ALTER TABLE 依赖旧表存在。
    if not _table_exists(db, "documents"):
        # 注意：这里不能用 executescript —— sqlite3 的 executescript 会先隐式提交，
        # 从而破坏迁移外层的事务，导致“cannot commit - no transaction is active”。
        _exec_script(db, """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
                created_at TEXT NOT NULL, status TEXT NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            CREATE TABLE chunks (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                page INTEGER NOT NULL, text TEXT NOT NULL
            );
            CREATE INDEX ix_chunks_document ON chunks(document_id);
            CREATE TABLE embedding_indexes (
                document_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                provider_signature TEXT, source_signature TEXT,
                dimension INTEGER, chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            CREATE TABLE embedding_vectors (
                document_id TEXT NOT NULL, chunk_id TEXT PRIMARY KEY, vector TEXT NOT NULL
            );
            CREATE INDEX ix_vectors_document ON embedding_vectors(document_id);
        """)
    # 旧表 embedding_indexes 必须先扩展并重建为版本化索引（documents 与向量表都引用其 id）。
    _add_columns(db, _MIGRATION_V1_INDEX_COLUMNS)
    _add_columns(db, _MIGRATION_V1_CHUNK_COLUMNS)
    _exec_script(db, _MIGRATION_V1_REBUILD_INDEX_TABLE)
    # 再建契约表，最后给 documents 增加指向新表的外键列。
    _exec_script(db, _MIGRATION_V1_TABLES)
    _add_columns(db, _MIGRATION_V1_DOCUMENT_COLUMNS)
    # 分块表必须在契约表之后重建：新结构引用 parse_versions，并允许 page 为空。
    _exec_script(db, _MIGRATION_V1_REBUILD_CHUNKS_TABLE)
    _exec_script(db, _MIGRATION_V1_CHUNK_INDEXES)
    _migrate_legacy_content(db, now)


def _exec_script(db: sqlite3.Connection, script: str) -> None:
    """逐条执行 SQL；对“已存在”错误保持幂等，其余错误照常抛出。"""
    for statement in script.split(";"):
        text = statement.strip()
        if not text:
            continue
        try:
            db.execute(text)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "already exists" in message or "duplicate column name" in message:
                logger.warning("迁移 v1 跳过已存在对象：%s", exc)
                continue
            raise


def _add_columns(db: sqlite3.Connection, script: str) -> None:
    """为旧表补充新列；列已存在时跳过，保证重复运行安全。"""
    for statement in script.split(";"):
        text = statement.strip()
        if not text:
            continue
        try:
            db.execute(text)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                continue
            raise


def _migrate_legacy_content(db: sqlite3.Connection, now: str) -> None:
    """把旧库中已解析的文档登记为 legacy 解析版本，并保留其索引状态。

    规则（对应任务书 A3）：
    - 文档 ID、分块 ID、分块内容与页码、向量、维度、提供方签名、索引状态全部保留；
    - 不重新解析、不调用 embedding；
    - 索引不完整、维度缺失或向量数量与块数不一致的旧索引不迁移为“有效”，
      而是标记 failed 并写明原因，避免把损坏数据伪装成可用索引；
    - 旧 source_signature 用旧算法验证并原样保留，同时记录算法版本，
      迁移不重算签名掩盖损坏；
    - 旧 parsing/indexing 中断状态不在这里统一改成失败：解析任务由 worker 恢复，
      索引中断状态改为 failed 并提示用户重试（旧实现没有可恢复的索引队列）。
    """
    documents = db.execute(
        "SELECT id, filename, status, page_count, chunk_count FROM documents"
    ).fetchall()
    for doc_id, _filename, status, page_count, chunk_count in documents:
        chunk_rows = db.execute(
            "SELECT id, document_id, page, text FROM chunks WHERE document_id=? ORDER BY rowid",
            (doc_id,),
        ).fetchall()
        version_id = f"legacy-{doc_id}"
        # 旧文档即使 status 不是 parsed，只要有分块也建立 legacy 版本，避免丢失已有内容。
        if chunk_rows and status in {"parsed", "parsing", "failed"}:
            db.execute(
                """INSERT OR IGNORE INTO parse_versions
                   (id, document_id, task_id, origin_hash, parser_name, parser_version,
                    config_summary, result_schema_version, result_hash, quality_status,
                    quality_summary, block_count, page_count, chunk_count,
                    result_json_path, markdown_path, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, doc_id, None, "", "legacy", None,
                 '{"migrated_from":"pre-versioning","chunking":"legacy-800-120"}',
                 "legacy-1", None, "warnings",
                 "旧库迁移生成的兼容版本：仅保留原有分块与页码，未重新解析，页码为旧逻辑页",
                 len(chunk_rows), page_count or 0, len(chunk_rows), None, None, now),
            )
            db.execute(
                "UPDATE chunks SET parse_version_id=?, order_index=?, chunk_type=COALESCE(chunk_type,'legacy') "
                "WHERE document_id=? AND parse_version_id IS NULL",
                (version_id, None, doc_id),
            )
            # 显式顺序：旧库只有 rowid，按 rowid 回填为 0..n-1，后续不再依赖 rowid。
            for order_index, (chunk_id,) in enumerate(
                db.execute("SELECT id FROM chunks WHERE document_id=? ORDER BY rowid", (doc_id,))
            ):
                db.execute("UPDATE chunks SET order_index=? WHERE id=?", (order_index, chunk_id))
            db.execute(
                "UPDATE documents SET active_parse_version_id=? WHERE id=? AND active_parse_version_id IS NULL",
                (version_id, doc_id),
            )
            if chunk_count != len(chunk_rows):
                # 旧表统计与真实行数不一致时以真实行数为准，并留痕便于排查。
                logger.warning("文档 %s 的 chunk_count=%s 与实际分块数 %s 不一致，已按实际值迁移",
                               doc_id, chunk_count, len(chunk_rows))
                db.execute("UPDATE documents SET chunk_count=? WHERE id=?", (len(chunk_rows), doc_id))

        _migrate_legacy_index(db, doc_id, version_id, chunk_rows, now)


def _migrate_legacy_index(db, doc_id, version_id, chunk_rows, now):
    """迁移旧索引记录：保留签名与维度，验证完整性后再决定是否仍可用。"""
    row = db.execute("SELECT * FROM embedding_indexes WHERE document_id=?", (doc_id,)).fetchone()
    if row is None:
        return
    columns = _columns(db, "embedding_indexes")
    record = dict(zip(columns, row)) if not isinstance(row, sqlite3.Row) else dict(row)
    status = record.get("status")
    index_id = f"legacy-index-{doc_id}"
    vector_rows = db.execute(
        "SELECT chunk_id, vector FROM embedding_vectors WHERE document_id=?", (doc_id,)
    ).fetchall()
    vector_by_chunk = {chunk_id: vector for chunk_id, vector in vector_rows}
    chunk_ids = [item[0] for item in chunk_rows]
    # 完整性判定：状态为 indexed、维度为正、向量数量等于分块数、每个分块都有向量。
    complete = (
        status == "indexed"
        and isinstance(record.get("dimension"), int) and record["dimension"] > 0
        and record.get("provider_signature")
        and len(chunk_ids) > 0
        and len(vector_by_chunk) == len(chunk_ids)
        and all(chunk_id in vector_by_chunk for chunk_id in chunk_ids)
    )
    if complete:
        migrated_status = "indexed"
        migrated_error = record.get("error")
        quality_note = "旧索引完整，按原签名算法验证后保留可用"
    elif status == "indexing":
        # 旧实现没有可恢复的索引队列，中断的构建不能继续，标记失败并提示重试。
        migrated_status = "failed"
        migrated_error = "索引任务在旧版本中被中断，请重新建立索引"
        quality_note = "旧索引中断状态迁移"
    elif status == "failed":
        migrated_status = "failed"
        migrated_error = record.get("error") or "旧索引构建失败"
        quality_note = "旧索引失败状态迁移"
    else:
        migrated_status = "failed"
        migrated_error = "旧索引记录不完整（缺少向量或维度），未迁移为可用索引"
        quality_note = "旧索引不完整，按失败迁移以避免误用"
    db.execute(
        """INSERT OR REPLACE INTO embedding_indexes
           (document_id, status, provider_signature, source_signature, dimension, chunk_count,
            error, id, parse_version_id, model_signature, source_signature_algo, created_at,
            activated_at, is_active, chunking_algo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (doc_id, migrated_status, record.get("provider_signature"), record.get("source_signature"),
         record.get("dimension"), len(chunk_ids), migrated_error, index_id, version_id,
         record.get("provider_signature"), SIGNATURE_ALGO_LEGACY, now,
         now if migrated_status == "indexed" else None, 1 if migrated_status == "indexed" else 0,
         "legacy-800-120"),
    )
    # 旧向量迁入版本化向量表，同时保留旧表以避免回滚时丢失数据。
    for chunk_id in chunk_ids:
        vector = vector_by_chunk.get(chunk_id)
        if vector is None:
            continue
        dimension = record.get("dimension") or 0
        db.execute(
            "INSERT OR IGNORE INTO embedding_vectors_v2(index_id, chunk_id, document_id, vector, dimension)"
            " VALUES (?,?,?,?,?)",
            (index_id, chunk_id, doc_id, vector, dimension),
        )
    if migrated_status == "indexed":
        db.execute("UPDATE documents SET active_index_id=? WHERE id=?", (index_id, doc_id))
    logger.info("旧索引迁移：文档=%s 状态=%s（%s）", doc_id, migrated_status, quality_note)


def _apply_v2(db: sqlite3.Connection) -> None:
    """保留全部旧行，解除每文档只有一条索引的限制，允许独立候选索引。"""
    schema = db.execute("SELECT sql FROM sqlite_master WHERE name='embedding_indexes'").fetchone()[0]
    schema = schema.replace('CREATE TABLE "embedding_indexes"', 'CREATE TABLE embedding_indexes_v2')
    schema = schema.replace('CREATE TABLE embedding_indexes (', 'CREATE TABLE embedding_indexes_v2 (')
    schema = schema.replace(',\n    UNIQUE (document_id)', '')
    db.execute(schema)
    db.execute("INSERT INTO embedding_indexes_v2 SELECT * FROM embedding_indexes")
    db.execute("DROP TABLE embedding_indexes")
    db.execute("ALTER TABLE embedding_indexes_v2 RENAME TO embedding_indexes")
    db.execute("ALTER TABLE embedding_indexes ADD COLUMN lease_expires_at TEXT")
    db.execute("UPDATE embedding_indexes SET lease_expires_at=? WHERE status='indexing'",
               (datetime.now(timezone.utc).isoformat(),))
    db.execute("CREATE INDEX ix_embedding_indexes_document ON embedding_indexes(document_id)")
    db.execute("CREATE UNIQUE INDEX ux_index_active ON embedding_indexes(document_id) WHERE is_active=1")
    db.execute("CREATE UNIQUE INDEX ux_index_building ON embedding_indexes(document_id) WHERE status='indexing'")
    db.execute("ALTER TABLE parse_tasks ADD COLUMN result_version_id TEXT REFERENCES parse_versions(id)")
    db.execute("UPDATE parse_tasks SET result_version_id=(SELECT id FROM parse_versions WHERE task_id=parse_tasks.id)")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise MigrationError("迁移后外键校验失败")


# 版本 -> 迁移函数；按顺序执行。
MIGRATIONS = {1: _apply_v1, 2: _apply_v2}


def run_migrations(db: sqlite3.Connection, *, backup_dir: Path | None = None,
                   db_path: Path | None = None) -> dict:
    """执行所有未应用的迁移，返回执行摘要。

    任何一步失败都会回滚该版本的改动并抛出 MigrationError；调用方据此停止启动。
    """
    current = detect_version(db)
    if current >= SCHEMA_VERSION:
        return {"from": current, "to": current, "applied": [], "backup": None}
    backup_path = None
    if backup_dir is not None and db_path is not None:
        backup_path = backup_database(db_path, backup_dir)
    db.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
           )"""
    )
    applied = []
    # 整轮升级原子完成；Web 与 worker 并发启动时，在锁内再次读取版本。
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        locked_version = detect_version(db)
        for version in range(locked_version + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise MigrationError(f"缺少数据库迁移 {version}")
            migration(db)
            MigrationHook.run(version)
            db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?,?)",
                       (version, datetime.now(timezone.utc).isoformat()))
            applied.append(version)
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise MigrationError("迁移后外键校验失败")
        db.execute("COMMIT")
    except Exception as exc:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise MigrationError(f"数据库迁移失败：{exc}；原库未改动"
                             + (f"，备份位于 {backup_path}" if backup_path else "")) from exc
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    return {"from": current, "to": SCHEMA_VERSION, "applied": applied,
            "backup": str(backup_path) if backup_path else None}


def restore_instructions(db_path: Path, backup_path: Path | None) -> str:
    """生成恢复说明，供启动失败时提示用户。"""
    lines = [
        f"数据库迁移失败，服务已停止启动，原库 {db_path} 未被修改。",
        "恢复步骤：",
        "1. 停止 Web 与 worker 进程，避免继续写入。",
        "2. 备份当前数据库文件（含同目录 -wal/-shm）。",
    ]
    if backup_path:
        lines.append(f"3. 将原库及其 -wal/-shm 一并移至保留目录，再把备份 {backup_path} 复制为 {db_path}；不能残留旧 WAL。")
    else:
        lines.append("3. 本次未生成备份（首次启动的空库无需备份）。")
    lines.append("4. 回到旧代码时必须同时恢复对应迁移前数据库；新库结构不能直接供旧代码使用。")
    return "\n".join(lines)


def ensure_backup_dir(data_dir: Path) -> Path:
    """备份目录固定放在数据目录下，便于随数据一起排除在 Git 之外。"""
    path = data_dir / "db-backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def copy_backup(src: Path, dst: Path) -> None:
    """测试与运维使用的一致性复制辅助。"""
    shutil.copy2(src, dst)


class MigrationHook:
    """迁移过程中的故障注入点：仅测试使用，默认不注册任何钩子。

    任务书 T02 要求验证“迁移中途异常不发布半完成结构”，因此需要在迁移事务内部
    人为抛出异常。生产代码不会注册钩子，行为与没有该类时完全一致。
    """

    hooks: dict[int, callable] = {}

    @classmethod
    def register(cls, version: int, hook) -> None:
        cls.hooks[version] = hook

    @classmethod
    def clear(cls) -> None:
        cls.hooks.clear()

    @classmethod
    def run(cls, version: int) -> None:
        hook = cls.hooks.get(version)
        if hook is not None:
            hook()
