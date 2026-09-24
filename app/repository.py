# SQLite 持久化层：文档、解析任务、解析版本、块、来源、告警与版本化索引的读写。
#
# 事务约定（对应任务书 A2）：
# - 每个连接启用外键校验，写操作使用短事务；
# - 网络请求期间绝不持有 SQLite 写事务；
# - 任务领取使用条件更新（原子领取）并写入执行令牌与租约，续租和最终写入都校验令牌。
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app import migrations
from app.schemas import (
    Block,
    Chunk,
    Document,
    IndexAttempt,
    IndexInfo,
    ParseTask,
    ParseVersion,
    QualityWarning,
    SourceLocation,
)

logger = logging.getLogger(__name__)


class TaskConflict(Exception):
    """并发创建解析任务或幂等键冲突，可安全返回给客户端。"""


def now_iso() -> str:
    """统一使用带时区的 UTC ISO 时间字符串，便于比较租约过期。"""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Repository:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        # 每次操作独立连接；正常退出提交事务，发生异常回滚，最后关闭连接。
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        # 外键校验必须在每个连接上显式启用，否则约束不会生效。
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # 初始化与迁移
    # ------------------------------------------------------------------
    def initialize(self) -> dict:
        """执行迁移并恢复中断状态；返回迁移摘要。

        迁移失败时抛出 MigrationError，由调用方停止启动并保留原库与备份。
        这里不再把 parsing/indexing 一律改成失败：解析任务由 worker 按租约恢复，
        索引中断状态在迁移中一次性处理（旧实现没有可恢复的索引队列）。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 迁移使用显式事务控制（isolation_level=None），避免 sqlite3 的隐式事务
        # 与迁移内的 BEGIN/COMMIT/ROLLBACK 冲突；业务读写仍使用 connect()。
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            summary = migrations.run_migrations(
                connection, backup_dir=migrations.ensure_backup_dir(self.path.parent), db_path=self.path)
        finally:
            connection.close()
        with self.connect() as db:
            # 启动时不修改任何任务状态：worker 通过租约过期自行恢复，
            # 避免 Web 每次启动把正在执行的新任务错误地标记为失败。
            self._recover_expired_leases(db)
        return summary

    def _recover_expired_leases(self, db: sqlite3.Connection):
        """按阶段恢复过期任务；远端 ID 可恢复，提交结果不确定时禁止自动重投。"""
        now = now_iso()
        rows = db.execute("SELECT * FROM parse_tasks WHERE status='running' AND "
                          "(lease_expires_at IS NULL OR lease_expires_at<=?)", (now,)).fetchall()
        for row in rows:
            cancelled = bool(row['cancel_requested'])
            uncertain = not row['upstream_task_id'] and row['stage'] == 'submitting'
            status = 'needs_attention' if cancelled or uncertain else 'queued'
            code = 'waiting_paused' if cancelled else ('submit_uncertain' if uncertain else None)
            message = ('等待已暂停，可明确恢复' if cancelled else
                       '提交时进程中断，远端是否接收不确定；请核实后明确重试' if uncertain else None)
            db.execute("UPDATE parse_tasks SET status=?, lease_token=NULL, lease_expires_at=NULL, "
                       "updated_at=?, error_code=?, error_message=? WHERE id=?",
                       (status, now, code, message, row['id']))
            db.execute("UPDATE parse_attempts SET status='failed', error_code='lease_expired', "
                       "finished_at=? WHERE task_id=? AND status='running'", (now, row['id']))

    # ------------------------------------------------------------------
    # 文档
    # ------------------------------------------------------------------
    def create(self, document: Document):
        # 参数绑定将数据与 SQL 分开；这里只保存元数据，不保存文件内容。
        values = document.model_dump()
        with self.connect() as db:
            db.execute(
                """INSERT INTO documents
                   (id, filename, size, created_at, status, page_count, chunk_count, error, format)
                   VALUES (:id,:filename,:size,:created_at,:status,:page_count,:chunk_count,:error,:format)""",
                values,
            )

    # 文档列表统一走这段 SQL：status 为派生值，任务与质量状态分别来自最新任务与活动版本。
    _DOCUMENT_SELECT = """
        SELECT d.*,
               v.quality_status AS active_quality_status,
               (SELECT id FROM parse_tasks t WHERE t.document_id=d.id
                 ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1) AS latest_task_id,
               (SELECT status FROM parse_tasks t WHERE t.document_id=d.id
                 ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1) AS latest_task_status,
               (SELECT stage FROM parse_tasks t WHERE t.document_id=d.id
                 ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1) AS latest_task_stage,
               (SELECT error_message FROM parse_tasks t WHERE t.document_id=d.id
                 ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1) AS latest_task_error,
               (SELECT COUNT(*) FROM parse_tasks t WHERE t.document_id=d.id AND t.status IN ('queued','running')) AS active_tasks,
               (SELECT COUNT(*) FROM parse_tasks t WHERE t.document_id=d.id AND t.status='failed') AS failed_tasks,
               (SELECT i.parse_version_id FROM embedding_indexes i
                 WHERE i.id=d.active_index_id AND i.status='indexed' LIMIT 1) AS indexed_version_id
        FROM documents d
        LEFT JOIN parse_versions v ON v.id = d.active_parse_version_id
    """

    def _document_from_row(self, row: sqlite3.Row) -> Document:
        """把数据库行转换为文档契约，并派生“内容可用性”状态。

        派生规则（对应任务书 3.1）：
        - 存在活动解析版本 → parsed（旧内容仍可用，即使最新任务失败）；
        - 否则有活动任务 → parsing；
        - 否则最新任务失败 → failed；
        - 其余保持数据库中的 uploaded/parsing。
        """
        has_version = bool(row["active_parse_version_id"])
        if has_version:
            status = "parsed"
        elif row["active_tasks"]:
            status = "parsing"
        elif row["failed_tasks"]:
            status = "failed"
        else:
            status = row["status"]
        indexed_version = row["indexed_version_id"] if "indexed_version_id" in row.keys() else None
        # 旧库没有 format 列：按文件名扩展名推断真实格式，并保留判定依据供页面显示。
        stored_format = row["format"]
        format_source = "数据库记录" if stored_format else None
        if not stored_format:
            suffix = Path(row["filename"]).suffix.lower().lstrip(".")
            stored_format = suffix if suffix in {"txt", "pdf", "docx", "xlsx"} else None
            format_source = "由旧库文件名推断（未做内容校验）" if stored_format else None
        return Document(
            id=row["id"], filename=row["filename"], size=row["size"], created_at=row["created_at"],
            status=status, page_count=row["page_count"], chunk_count=row["chunk_count"],
            error=row["error"], format=stored_format,
            format_source=format_source,
            active_parse_version_id=row["active_parse_version_id"],
            active_index_id=row["active_index_id"],
            task_status=row["latest_task_status"] if "latest_task_status" in row.keys() else None,
            task_stage=row["latest_task_stage"] if "latest_task_stage" in row.keys() else None,
            quality_status=row["active_quality_status"] if "active_quality_status" in row.keys() else None,
            latest_task_id=row["latest_task_id"] if "latest_task_id" in row.keys() else None,
            latest_task_error=row["latest_task_error"] if "latest_task_error" in row.keys() else None,
            index_version_mismatch=bool(
                indexed_version and row["active_parse_version_id"]
                and indexed_version != row["active_parse_version_id"]
            ),
        )

    def list_documents(self) -> list[Document]:
        # 按上传时间倒序返回，工作台优先展示最新资料。
        with self.connect() as db:
            rows = db.execute(self._DOCUMENT_SELECT + " ORDER BY d.created_at DESC").fetchall()
        return [self._document_from_row(row) for row in rows]

    def get(self, document_id: str) -> Document | None:
        # 存储层用 None 表示不存在，由接口层决定 HTTP 响应。
        with self.connect() as db:
            row = db.execute(self._DOCUMENT_SELECT + " WHERE d.id=?", (document_id,)).fetchone()
        return self._document_from_row(row) if row else None

    def update_document_status(self, document_id: str, *, status: str | None = None, error: str | None = None,
                               page_count: int | None = None, chunk_count: int | None = None):
        """按需更新文档冗余字段；任务与版本状态不再通过这里改写。"""
        assignments, values = [], []
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if error is not None:
            assignments.append("error=?")
            values.append(error)
        if page_count is not None:
            assignments.append("page_count=?")
            values.append(page_count)
        if chunk_count is not None:
            assignments.append("chunk_count=?")
            values.append(chunk_count)
        if not assignments:
            return
        values.append(document_id)
        with self.connect() as db:
            db.execute(f"UPDATE documents SET {', '.join(assignments)} WHERE id=?", values)

    def set_active_version(self, document_id: str, version_id: str, *, page_count: int, chunk_count: int):
        """发布新解析版本时更新活动版本指针（由 publish_parse_version 在同一事务调用）。"""
        with self.connect() as db:
            db.execute(
                "UPDATE documents SET active_parse_version_id=?, page_count=?, chunk_count=?,"
                " status='parsed', error=NULL WHERE id=?",
                (version_id, page_count, chunk_count, document_id),
            )

    # ------------------------------------------------------------------
    # 解析任务
    # ------------------------------------------------------------------
    _TASK_FIELDS = ("id", "document_id", "status", "stage", "idempotency_key", "request_summary",
                    "upstream_task_id", "attempt_count", "max_attempts", "lease_token",
                    "lease_expires_at", "error_code", "error_message", "created_at", "updated_at",
                    "started_at", "finished_at")

    def _task_from_row(self, row: sqlite3.Row) -> ParseTask:
        summary = None
        if row["request_summary"]:
            try:
                summary = json.loads(row["request_summary"])
            except (TypeError, ValueError):
                summary = None
        # 兼容调用方未带 request_fingerprint 列的查询：指纹保存在请求摘要中，
        # 所有任务查询都必须带上 request_summary，否则幂等键冲突检测会被跳过。
        result_version_id = None
        if "result_version_id" in row.keys():
            result_version_id = row["result_version_id"]
        stage_detail = None
        if "stage_detail" in row.keys():
            stage_detail = row["stage_detail"]
        return ParseTask(
            id=row["id"], document_id=row["document_id"], status=row["status"], stage=row["stage"],
            idempotency_key=row["idempotency_key"], request_summary=summary,
            upstream_task_id=row["upstream_task_id"], attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"], lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"], error_code=row["error_code"],
            error_message=row["error_message"], created_at=row["created_at"], updated_at=row["updated_at"],
            started_at=row["started_at"], finished_at=row["finished_at"],
            result_version_id=result_version_id, stage_detail=stage_detail,
        )

    def create_task(self, task: ParseTask) -> ParseTask:
        """新建任务；同一文档已有活动任务时由数据库唯一索引拒绝并发插入。"""
        with self.connect() as db:
            # 检查与写入处于同一写事务，避免两个 HTTP 请求同时穿过预检查。
            db.execute("BEGIN IMMEDIATE")
            if task.idempotency_key:
                old = db.execute("SELECT * FROM parse_tasks WHERE document_id=? AND idempotency_key=?",
                                 (task.document_id, task.idempotency_key)).fetchone()
                if old:
                    previous = self._task_from_row(old)
                    if (previous.request_summary or {}).get('fingerprint') != (task.request_summary or {}).get('fingerprint'):
                        raise TaskConflict("幂等键已用于不同请求，请更换幂等键")
                    return previous
            active = db.execute("SELECT * FROM parse_tasks WHERE document_id=? AND status IN ('queued','running')",
                                (task.document_id,)).fetchone()
            if active:
                return self._task_from_row(active)
            db.execute(
                """INSERT INTO parse_tasks
                   (id, document_id, status, stage, idempotency_key, request_fingerprint,
                    request_summary, upstream_task_id, attempt_count, max_attempts, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task.id, task.document_id, task.status, task.stage, task.idempotency_key,
                 (task.request_summary or {}).get("fingerprint"),
                 json.dumps(task.request_summary or {}, ensure_ascii=False),
                 task.upstream_task_id, task.attempt_count, task.max_attempts,
                 task.created_at, task.updated_at),
            )
        return self.get_task(task.id)

    def get_task(self, task_id: str) -> ParseTask | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT t.*
                   FROM parse_tasks t WHERE t.id=?""", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def find_active_task(self, document_id: str) -> ParseTask | None:
        """返回该文档当前的活动任务（queued/running），用于重复点击复用。"""
        with self.connect() as db:
            row = db.execute(
                """SELECT t.*
                   FROM parse_tasks t WHERE t.document_id=? AND t.status IN ('queued','running')
                   ORDER BY t.created_at DESC LIMIT 1""", (document_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def latest_task(self, document_id: str) -> ParseTask | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT t.*
                   FROM parse_tasks t WHERE t.document_id=?
                   ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1""", (document_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def find_task_by_idempotency(self, document_id: str, key: str) -> ParseTask | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT t.*
                   FROM parse_tasks t WHERE t.document_id=? AND t.idempotency_key=?""",
                (document_id, key)).fetchone()
        return self._task_from_row(row) if row else None

    def claim_next_task(self, worker_id: str, lease_seconds: int, *, task_id: str | None = None) -> ParseTask | None:
        """原子领取一个可执行任务。

        领取条件：status='queued'，或 status='running' 且租约已过期（上一个执行者已失效）。
        领取时写入新的执行令牌与租约；旧令牌因此无法继续写入或发布结果。
        """
        now = datetime.now(timezone.utc)
        token = f"{worker_id}:{now.timestamp()}"
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_expired_leases(db)
            if task_id:
                row = db.execute(
                    "SELECT * FROM parse_tasks WHERE id=? AND status='queued' AND cancel_requested=0",
                    (task_id,)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM parse_tasks WHERE status='queued' AND cancel_requested=0"
                    " ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            updated = db.execute(
                """UPDATE parse_tasks SET status='running', lease_token=?, lease_expires_at=?,
                   heartbeat_at=?, started_at=COALESCE(started_at,?), updated_at=?,
                   attempt_count=attempt_count+1
                   WHERE id=? AND (status='queued' OR (status='running' AND lease_expires_at < ?))""",
                (token, expires, now.isoformat(), now.isoformat(), now.isoformat(),
                 row["id"], now.isoformat()))
            if updated.rowcount != 1:
                # 另一个 worker 抢先领取；本次不重复执行。
                return None
            db.execute(
                """INSERT INTO parse_attempts(id, task_id, attempt_no, status, stage, lease_token,
                   upstream_task_id, started_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (f"att-{row['id']}-{row['attempt_count'] + 1}", row["id"], row["attempt_count"] + 1,
                 "running", "submitting", token, row["upstream_task_id"], now.isoformat()),
            )
        return self.get_task(row["id"])

    def renew_lease(self, task_id: str, token: str, lease_seconds: int) -> bool:
        """续租；只有当前令牌持有者可以续租，过期执行者续租失败后不得继续写入。"""
        now = datetime.now(timezone.utc)
        with self.connect() as db:
            result = db.execute(
                "UPDATE parse_tasks SET lease_expires_at=?, heartbeat_at=?, updated_at=?"
                " WHERE id=? AND lease_token=? AND status='running' AND lease_expires_at>?",
                ((now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(),
                 now.isoformat(), task_id, token, now.isoformat()))
            return result.rowcount == 1

    def update_task_progress(self, task_id: str, token: str, *, stage: str,
                             upstream_task_id: str | None = None) -> bool:
        """更新执行阶段与上游 task_id；必须在持有有效令牌时调用。"""
        with self.connect() as db:
            result = db.execute(
                "UPDATE parse_tasks SET stage=?, updated_at=?,"
                " upstream_task_id=COALESCE(?, upstream_task_id)"
                " WHERE id=? AND lease_token=? AND status='running' AND lease_expires_at>?",
                (stage, now_iso(), upstream_task_id, task_id, token, now_iso()))
            if result.rowcount == 1 and upstream_task_id:
                db.execute(
                    "UPDATE parse_attempts SET upstream_task_id=?, stage=?"
                    " WHERE task_id=? AND lease_token=? AND status='running'",
                    (upstream_task_id, stage, task_id, token))
            return result.rowcount == 1

    def task_token_valid(self, db: sqlite3.Connection, task_id: str, token: str) -> bool:
        row = db.execute("SELECT lease_token, status, lease_expires_at FROM parse_tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row["status"] == "running" and row["lease_token"] == token
                    and row["lease_expires_at"] and row["lease_expires_at"] > now_iso())

    def finish_task(self, task_id: str, token: str, *, status: str, stage: str,
                    error_code: str | None = None, error_message: str | None = None) -> bool:
        """写入任务终态；令牌失效时拒绝写入，避免过期执行者覆盖新执行结果。"""
        now = now_iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.task_token_valid(db, task_id, token):
                return False
            db.execute(
                "UPDATE parse_tasks SET status=?, stage=?, error_code=?, error_message=?,"
                " finished_at=?, updated_at=?, lease_token=NULL, lease_expires_at=NULL"
                " WHERE id=? AND lease_token=?",
                (status, stage, error_code, error_message, now, now, task_id, token))
            db.execute(
                "UPDATE parse_attempts SET status=?, stage=?, error_code=?, error_message=?, finished_at=?"
                " WHERE task_id=? AND lease_token=? AND status='running'",
                (status, stage, error_code, error_message, now, task_id, token))
        return True

    def abandon_queued_task(self, task_id: str, message: str = "任务在开始执行前被取消") -> bool:
        """取消一个尚未被任何 worker 领取的排队任务。

        与 finish_task 的区别：排队任务没有租约令牌，因此这里不校验令牌，
        但只允许作用于 status='queued' 的任务，绝不会覆盖正在执行的任务结果。
        """
        now = now_iso()
        with self.connect() as db:
            result = db.execute(
                "UPDATE parse_tasks SET status='failed', stage='done', error_code='cancelled_before_run',"
                " error_message=?, finished_at=?, updated_at=? WHERE id=? AND status='queued'",
                (message, now, now, task_id))
            return result.rowcount == 1

    def request_cancel(self, task_id: str) -> bool:
        """请求取消：worker 在轮询间隙读取该标记后优雅退出并保留可恢复状态。"""
        with self.connect() as db:
            result = db.execute(
                "UPDATE parse_tasks SET cancel_requested=1, updated_at=? WHERE id=? AND status IN ('queued','running')",
                (now_iso(), task_id))
            db.execute("UPDATE parse_tasks SET status='needs_attention', error_code='waiting_paused', "
                       "error_message='等待已暂停，可明确恢复' WHERE id=? AND status='queued' AND cancel_requested=1",
                       (task_id,))
            return result.rowcount == 1

    def task_cancelled(self, task_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT cancel_requested FROM parse_tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def mark_task_needs_attention(self, task_id: str, token: str, code: str, message: str) -> bool:
        """提交结果不确定：保留恢复信息，禁止盲目自动重投。"""
        now = now_iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.task_token_valid(db, task_id, token):
                return False
            db.execute(
                "UPDATE parse_tasks SET status='needs_attention', error_code=?, error_message=?,"
                " updated_at=?, lease_token=NULL, lease_expires_at=NULL WHERE id=?",
                (code, message, now, task_id))
            db.execute(
                "UPDATE parse_attempts SET status='needs_attention', error_code=?, error_message=?,"
                " finished_at=? WHERE task_id=? AND lease_token=? AND status='running'",
                (code, message, now, task_id, token))
        return True

    def queued_task_count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM parse_tasks WHERE status='queued'").fetchone()[0]

    # ------------------------------------------------------------------
    # 解析版本、块、来源与告警
    # ------------------------------------------------------------------
    @staticmethod
    def _reuse_version(db, task_id, document_id, version_id):
        """相同内容复用不可变版本，索引指针不随预览切换。"""
        version = db.execute("SELECT page_count, chunk_count FROM parse_versions WHERE id=?", (version_id,)).fetchone()
        db.execute("UPDATE documents SET active_parse_version_id=?, page_count=?, chunk_count=?, "
                   "status='parsed', error=NULL WHERE id=?", (version_id, version['page_count'], version['chunk_count'], document_id))
        db.execute("UPDATE parse_tasks SET result_version_id=? WHERE id=?", (version_id, task_id))

    def publish_parse_version(self, task_id: str, token: str, version: ParseVersion,
                              blocks: list[Block], chunks: list[Chunk],
                              warnings: list[QualityWarning]) -> tuple[str | None, bool]:
        """在短事务中发布解析版本、块、来源、告警与分块，并切换活动版本指针。

        返回 (version_id, created)。version_id 为 None 表示令牌已失效，本次结果被丢弃。
        幂等：同一任务同一 result_hash 已发布时直接复用，不重复写入块与分块。
        """
        now = now_iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.task_token_valid(db, task_id, token):
                # 过期执行者不得写入或切换活动版本。
                logger.warning("任务 %s 的租约已失效，丢弃本次结果", task_id)
                return None, False
            if version.result_hash:
                existing = db.execute(
                    "SELECT id FROM parse_versions WHERE document_id=? AND result_hash=?",
                    (version.document_id, version.result_hash)).fetchone()
                if existing:
                    # 结果已发布（重复领取或上次进程在发布后中断）：复用，不重复生成块。
                    db.execute(
                        "UPDATE parse_tasks SET stage='done', updated_at=?, error_code=NULL, error_message=NULL"
                        " WHERE id=? AND lease_token=?", (now, task_id, token))
                    self._reuse_version(db, task_id, version.document_id, existing["id"])
                    return existing["id"], False
            # 同一版本 ID 已存在（上次发布后、任务落终态前进程中断）：直接复用，
            # 不重复插入块、来源、告警与分块，避免重复发布与唯一约束冲突。
            if db.execute("SELECT 1 FROM parse_versions WHERE id=?", (version.id,)).fetchone():
                db.execute(
                    "UPDATE parse_tasks SET stage='done', updated_at=?, error_code=NULL, error_message=NULL"
                    " WHERE id=? AND lease_token=?", (now, task_id, token))
                self._reuse_version(db, task_id, version.document_id, version.id)
                return version.id, False
            db.execute(
                """INSERT INTO parse_versions
                   (id, document_id, task_id, origin_hash, parser_name, parser_version, config_summary,
                    result_schema_version, result_hash, quality_status, quality_summary, block_count,
                    page_count, chunk_count, result_json_path, markdown_path, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version.id, version.document_id, task_id, version.origin_hash, version.parser_name,
                 version.parser_version, version.config_summary, version.result_schema_version,
                 version.result_hash, version.quality_status, version.quality_summary,
                 len(blocks), version.page_count, len(chunks),
                 version.result_json_path, version.markdown_path, version.created_at))
            for block in blocks:
                db.execute(
                    """INSERT INTO blocks(id, document_id, parse_version_id, order_index, block_type, label,
                       text, heading_path, table_json, node_ref, sheet_name, char_count, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (block.id, block.document_id, block.parse_version_id, block.order_index,
                     block.block_type, block.label, block.text, block.heading_path,
                     json.dumps(block.table, ensure_ascii=False) if block.table else None,
                     None, block.sheet_name,
                     len(block.text), now))
                for ordinal, source in enumerate(block.sources):
                    db.execute(
                        """INSERT INTO sources(id, document_id, parse_version_id, block_id, ordinal, format,
                           page_no, page_end, bbox_json, coord_origin, coord_unit, section_path, table_no,
                           row_index, col_index, row_span, col_span, sheet_name, cell_range, line_start,
                           line_end, node_ref, char_start, char_end, note)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"{block.id}-src{ordinal}", block.document_id, block.parse_version_id, block.id,
                         ordinal, source.format, source.page, source.page_end,
                         json.dumps(source.bbox, ensure_ascii=False) if source.bbox else None,
                         source.coord_origin, source.coord_unit, source.section_path, source.table_no,
                         source.row_index, source.col_index, source.row_span, source.col_span,
                         source.sheet_name, source.cell_range, source.line_start, source.line_end,
                         source.node_ref, source.char_start, source.char_end, source.note))
            for ordinal, warning in enumerate(warnings):
                db.execute(
                    """INSERT INTO quality_warnings(id, document_id, parse_version_id, code, message,
                       severity, scope, block_id, source_json, page_no, sheet_name, detail, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    # 告警 ID 必须唯一：同一码同一页可能出现多条（例如多个空白页或多条目录告警），
                    # 因此把序号也编入 ID，避免唯一约束冲突导致整个版本无法发布。
                    (f"w-{version.id}-{ordinal}-{warning.code}",
                     version.document_id, version.id, warning.code, warning.message, warning.severity,
                     warning.scope, warning.block_id,
                     json.dumps(warning.source.model_dump(), ensure_ascii=False) if warning.source else None,
                     warning.page, warning.sheet_name, warning.detail, now))
            for chunk in chunks:
                db.execute(
                    """INSERT INTO chunks(id, document_id, page, text, parse_version_id, block_id,
                       order_index, chunk_type, sources_json, char_count)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (chunk.id, chunk.document_id, chunk.page, chunk.text, version.id,
                     chunk.block_id, chunk.order_index, chunk.chunk_type,
                     json.dumps([s.model_dump() for s in chunk.sources], ensure_ascii=False),
                     len(chunk.text)))
            db.execute(
                "UPDATE documents SET active_parse_version_id=?, page_count=?, chunk_count=?,"
                " status='parsed', error=NULL WHERE id=?",
                (version.id, version.page_count, len(chunks), version.document_id))
            # 旧解析版本与其向量在发布新版本后不再可用（向量必须绑定唯一分块），
            # 与 finish_parse 共用同一退役逻辑，避免出现悬空向量或跨版本混用。
            db.execute("UPDATE parse_tasks SET result_version_id=? WHERE id=?", (version.id, task_id))
            db.execute(
                "UPDATE parse_tasks SET stage='done', updated_at=? WHERE id=? AND lease_token=?",
                (now, task_id, token))
        return version.id, True

    def get_parse_version(self, version_id: str) -> ParseVersion | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM parse_versions WHERE id=?", (version_id,)).fetchone()
            if row is None:
                return None
            warnings = self._warnings_for(db, version_id)
        return self._version_from_row(row, warnings)

    def list_parse_versions(self, document_id: str) -> list[ParseVersion]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM parse_versions WHERE document_id=? ORDER BY created_at DESC",
                (document_id,)).fetchall()
            return [self._version_from_row(row, self._warnings_for(db, row["id"])) for row in rows]

    def _warnings_for(self, db: sqlite3.Connection, version_id: str) -> list[QualityWarning]:
        rows = db.execute(
            "SELECT * FROM quality_warnings WHERE parse_version_id=? ORDER BY severity DESC, code",
            (version_id,)).fetchall()
        warnings = []
        for row in rows:
            source = None
            if row["source_json"]:
                try:
                    source = SourceLocation(**json.loads(row["source_json"]))
                except (TypeError, ValueError):
                    source = None
            warnings.append(QualityWarning(
                code=row["code"], message=row["message"], severity=row["severity"], scope=row["scope"],
                block_id=row["block_id"], source=source, page=row["page_no"],
                sheet_name=row["sheet_name"], detail=row["detail"]))
        return warnings

    def _version_from_row(self, row: sqlite3.Row, warnings: list[QualityWarning]) -> ParseVersion:
        return ParseVersion(
            id=row["id"], document_id=row["document_id"], task_id=row["task_id"],
            origin_hash=row["origin_hash"], parser_name=row["parser_name"],
            parser_version=row["parser_version"], config_summary=row["config_summary"],
            result_schema_version=row["result_schema_version"], quality_status=row["quality_status"],
            quality_summary=row["quality_summary"], block_count=row["block_count"],
            result_hash=row["result_hash"],
            page_count=row["page_count"], chunk_count=row["chunk_count"], created_at=row["created_at"],
            has_structured_result=bool(row["result_json_path"]), has_markdown=bool(row["markdown_path"]),
            warnings=warnings, is_legacy=row["parser_name"] == "legacy",
        )

    def version_result_paths(self, version_id: str) -> tuple[str | None, str | None]:
        """返回结构化结果与 Markdown 的相对路径（不返回绝对路径给接口层）。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT result_json_path, markdown_path FROM parse_versions WHERE id=?",
                (version_id,)).fetchone()
        return (row["result_json_path"], row["markdown_path"]) if row else (None, None)

    def blocks(self, version_id: str, *, limit: int | None = None, offset: int = 0) -> list[Block]:
        """按显式顺序读取块；来源一次取出后按块分组，避免 N+1 查询。"""
        with self.connect() as db:
            sql = "SELECT * FROM blocks WHERE parse_version_id=? ORDER BY order_index"
            params: list = [version_id]
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params += [limit, offset]
            rows = db.execute(sql, params).fetchall()
            source_rows = db.execute(
                "SELECT * FROM sources WHERE parse_version_id=? ORDER BY block_id, ordinal",
                (version_id,)).fetchall()
        by_block: dict[str, list[SourceLocation]] = {}
        for row in source_rows:
            by_block.setdefault(row["block_id"], []).append(self._source_from_row(row))
        blocks = []
        for row in rows:
            table = None
            if row["table_json"]:
                try:
                    table = json.loads(row["table_json"])
                except (TypeError, ValueError):
                    table = None
            blocks.append(Block(
                id=row["id"], document_id=row["document_id"], parse_version_id=row["parse_version_id"],
                order_index=row["order_index"], block_type=row["block_type"], label=row["label"],
                text=row["text"], heading_path=row["heading_path"], table=table,
                sources=by_block.get(row["id"], []), char_count=row["char_count"]))
        return blocks

    def _source_from_row(self, row: sqlite3.Row) -> SourceLocation:
        bbox = None
        if row["bbox_json"]:
            try:
                bbox = json.loads(row["bbox_json"])
            except (TypeError, ValueError):
                bbox = None
        return SourceLocation(
            format=row["format"], node_ref=row["node_ref"], page=row["page_no"], page_end=row["page_end"],
            bbox=bbox, coord_origin=row["coord_origin"], coord_unit=row["coord_unit"],
            section_path=row["section_path"], table_no=row["table_no"], row_index=row["row_index"],
            col_index=row["col_index"], row_span=row["row_span"], col_span=row["col_span"],
            sheet_name=row["sheet_name"], cell_range=row["cell_range"], line_start=row["line_start"],
            line_end=row["line_end"], char_start=row["char_start"], char_end=row["char_end"],
            note=row["note"])

    def finish_parse(self, document_id: str, pages: int, chunks: list[Chunk]) -> str:
        """在短事务中写入一个解析版本及其分块，并切换活动版本指针。

        保留该入口用于“本地直接写入版本”（测试夹具、脚本导入）：
        - 自动创建与本次内容绑定的解析版本，避免出现没有版本归属的分块；
        - 不删除旧版本分块与向量：旧索引仍可检索，符合“新任务失败不等于旧内容失效”；
        - 旧的 embedding_vectors 兼容表在发布新版本时清空，防止旧向量被误用。
        """
        version_id = chunks[0].parse_version_id if chunks and chunks[0].parse_version_id else None
        version_id = version_id or f"v-{document_id[:12]}-{uuid4().hex[:16]}"
        now = now_iso()
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO parse_versions
                   (id, document_id, task_id, origin_hash, parser_name, parser_version, config_summary,
                    result_schema_version, result_hash, quality_status, quality_summary, block_count,
                    page_count, chunk_count, result_json_path, markdown_path, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, document_id, None, "", "direct-write", None,
                 '{"source":"repository.finish_parse"}', "direct-1", None, "ok",
                 "由本地直接写入的解析版本", len(chunks), pages, len(chunks), None, None, now))
            for order_index, chunk in enumerate(chunks):
                db.execute(
                    """INSERT OR REPLACE INTO chunks
                       (id, document_id, page, text, parse_version_id, block_id, order_index,
                        chunk_type, sources_json, char_count)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (chunk.id, chunk.document_id, chunk.page, chunk.text, version_id, chunk.block_id,
                     order_index, chunk.chunk_type or "text",
                     json.dumps([s.model_dump() for s in chunk.sources], ensure_ascii=False),
                     len(chunk.text)))
            # 先把活动版本指针指向新版本，再退役旧版本，避免删除时仍被 documents 引用。
            db.execute(
                "UPDATE documents SET active_parse_version_id=?, page_count=?, chunk_count=?,"
                " status='parsed', error=NULL WHERE id=?",
                (version_id, pages, len(chunks), document_id))

        return version_id

    def chunks(self, document_id: str, version_id: str | None = None) -> list[Chunk]:
        with self.connect() as db:
            if version_id is None:
                row = db.execute(
                    "SELECT active_parse_version_id FROM documents WHERE id=?", (document_id,)).fetchone()
                version_id = row["active_parse_version_id"] if row else None
            if version_id is None:
                # 旧库尚未建立活动版本时退回旧行为，保持兼容。
                rows = db.execute(
                    "SELECT * FROM chunks WHERE document_id=? ORDER BY rowid", (document_id,)).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM chunks WHERE document_id=? AND parse_version_id=?"
                    " ORDER BY order_index, rowid", (document_id, version_id)).fetchall()
        return [self._chunk_from_row(row) for row in rows]

    def _chunk_from_row(self, row: sqlite3.Row) -> Chunk:
        sources: list[SourceLocation] = []
        keys = row.keys()
        if "sources_json" in keys and row["sources_json"]:
            try:
                sources = [SourceLocation(**item) for item in json.loads(row["sources_json"])]
            except (TypeError, ValueError):
                sources = []
        if not sources and row["chunk_type"] == 'legacy':
            # 旧库或 legacy 版本的分块没有来源列：按旧语义构造兼容来源，
            # 并说明这是旧逻辑页，避免把旧页码当成当前解析的物理页来源。
            sources = [SourceLocation(format="txt", page=row["page"],
                                      note="旧库迁移的兼容来源：页码为旧解析逻辑页")]
        return Chunk(
            id=row["id"], document_id=row["document_id"], page=row["page"], text=row["text"],
            parse_version_id=row["parse_version_id"] if "parse_version_id" in keys else None,
            order_index=row["order_index"] if "order_index" in keys else None,
            block_id=row["block_id"] if "block_id" in keys else None,
            chunk_type=row["chunk_type"] if "chunk_type" in keys else None,
            heading_path=next((s.section_path for s in sources if s.section_path), None), sources=sources)

    # ------------------------------------------------------------------
    # 版本化索引
    # ------------------------------------------------------------------
    def active_index(self, document_id: str) -> IndexInfo | None:
        """返回当前可用索引（含绑定版本与构建尝试），不可用时返回 None。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM embedding_indexes WHERE document_id=? AND status='indexed' AND is_active=1", (document_id,)).fetchone()
            if row is None:
                return None
            attempts = self._attempts_for(db, document_id)
            active_version = db.execute(
                "SELECT active_parse_version_id FROM documents WHERE id=?", (document_id,)).fetchone()
        return self._index_from_row(row, attempts,
                                    active_version["active_parse_version_id"] if active_version else None)

    def index_row(self, document_id: str) -> IndexInfo | None:
        """返回该文档最新一条索引记录，无论其状态（用于展示失败或构建中）。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM embedding_indexes WHERE document_id=? ORDER BY rowid DESC LIMIT 1",
                (document_id,)).fetchone()
            if row is None:
                return None
            attempts = self._attempts_for(db, document_id)
            active_version = db.execute(
                "SELECT active_parse_version_id FROM documents WHERE id=?", (document_id,)).fetchone()
        return self._index_from_row(row, attempts,
                                    active_version["active_parse_version_id"] if active_version else None)

    def _attempts_for(self, db: sqlite3.Connection, document_id: str, limit: int = 10) -> list[IndexAttempt]:
        rows = db.execute(
            "SELECT * FROM index_attempts WHERE document_id=? ORDER BY started_at DESC LIMIT ?",
            (document_id, limit)).fetchall()
        return [IndexAttempt(
            id=row["id"], document_id=row["document_id"], index_id=row["index_id"],
            target_parse_version_id=row["target_parse_version_id"], status=row["status"],
            provider_signature=row["provider_signature"], source_signature=row["source_signature"],
            source_signature_algo=row["source_signature_algo"], dimension=row["dimension"],
            chunk_count=row["chunk_count"], error_code=row["error_code"],
            error_message=row["error_message"], started_at=row["started_at"],
            finished_at=row["finished_at"]) for row in rows]

    def _index_from_row(self, row: sqlite3.Row, attempts: list[IndexAttempt],
                        active_version_id: str | None) -> IndexInfo:
        keys = row.keys()
        version_id = row["parse_version_id"] if "parse_version_id" in keys else None
        return IndexInfo(
            id=row["id"] if "id" in keys else None, document_id=row["document_id"],
            parse_version_id=version_id, status=row["status"],
            model_signature=row["provider_signature"], source_signature=row["source_signature"],
            source_signature_algo=(row["source_signature_algo"] if "source_signature_algo" in keys else None),
            dimension=row["dimension"], chunk_count=row["chunk_count"], error=row["error"],
            created_at=row["created_at"] if "created_at" in keys else None,
            activated_at=row["activated_at"] if "activated_at" in keys else None,
            matches_active_version=bool(version_id and version_id == active_version_id),
            is_legacy=bool(version_id and str(version_id).startswith("legacy-")),
            attempts=attempts,
        )

    def begin_index_attempt(self, document_id: str, index_id: str, target_version_id: str,
                            model_signature: str) -> str:
        """登记一次索引构建尝试，并把索引置为 indexing；失败不改变已有成功索引的可用状态。"""
        attempt_id = f"idx-{document_id}-{datetime.now(timezone.utc).timestamp()}"
        with self.connect() as db:
            db.execute(
                """INSERT INTO index_attempts(id, document_id, index_id, target_parse_version_id,
                   provider_signature, status, started_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (attempt_id, document_id, index_id, target_version_id, model_signature,
                 "indexing", now_iso()))
        return attempt_id

    def activate_index(self, document_id: str, index_id: str, attempt_id: str, *,
                       version_id: str, model_signature: str, source_signature: str,
                       source_signature_algo: str, dimension: int, chunk_count: int,
                       vectors: list[tuple[str, str, int]], chunking_algo: str,
                       expected_version_id: str) -> bool:
        """原子发布候选索引：核对目标解析版本未变化后，写入向量并切换活动索引。

        返回 False 表示目标解析版本已变化（例如建 B 索引期间 C 成为活动版本），
        此时本次构建标记为 superseded，原活动索引保持不变。
        """
        now = now_iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT active_parse_version_id FROM documents WHERE id=?", (document_id,)).fetchone()
            if current is None or current["active_parse_version_id"] != expected_version_id:
                db.execute(
                    "UPDATE index_attempts SET status='superseded', error_code='target_version_changed',"
                    " error_message='构建期间活动解析版本已变化，本次构建已过期，原索引保持可用',"
                    " finished_at=? WHERE id=?", (now, attempt_id))
                db.execute("UPDATE embedding_indexes SET status='superseded' WHERE id=?", (index_id,))
                return False
            candidate = db.execute("SELECT status, lease_expires_at FROM embedding_indexes WHERE id=?", (index_id,)).fetchone()
            if (candidate is None or candidate['status'] != 'indexing'
                    or not candidate['lease_expires_at'] or candidate['lease_expires_at'] <= now):
                return False
            db.execute("UPDATE embedding_indexes SET is_active=0 WHERE document_id=?", (document_id,))
            db.execute("DELETE FROM embedding_vectors_v2 WHERE index_id=?", (index_id,))
            for chunk_id, vector, dimension_value in vectors:
                db.execute(
                    """INSERT INTO embedding_vectors_v2(index_id, chunk_id, document_id, vector, dimension)
                       VALUES (?,?,?,?,?)""",
                    (index_id, chunk_id, document_id, vector, dimension_value))
            # 旧表同步写入，保证旧代码回滚时仍可读取；两份数据在同一事务内保持一致。
            db.execute("DELETE FROM embedding_vectors WHERE document_id=?", (document_id,))
            db.executemany(
                "INSERT INTO embedding_vectors(document_id, chunk_id, vector) VALUES (?,?,?)",
                [(document_id, chunk_id, vector) for chunk_id, vector, _ in vectors])
            db.execute(
                """UPDATE embedding_indexes SET status='indexed', provider_signature=?, source_signature=?,
                   source_signature_algo=?, dimension=?, chunk_count=?, error=NULL, activated_at=?,
                   is_active=1, chunking_algo=? WHERE id=?""",
                (model_signature, source_signature, source_signature_algo, dimension, chunk_count,
                 now, chunking_algo, index_id))
            db.execute(
                "UPDATE index_attempts SET status='indexed', source_signature=?, source_signature_algo=?,"
                " dimension=?, chunk_count=?, finished_at=?, error_code=NULL, error_message=NULL WHERE id=?",
                (source_signature, source_signature_algo, dimension, chunk_count, now, attempt_id))
            db.execute("UPDATE documents SET active_index_id=? WHERE id=?", (index_id, document_id))
        return True

    def fail_index_attempt(self, attempt_id: str, *, code: str, message: str, document_id: str):
        """构建失败：只更新尝试记录与失败原因，不修改原成功索引的可用状态。"""
        with self.connect() as db:
            db.execute(
                "UPDATE index_attempts SET status='failed', error_code=?, error_message=?, finished_at=?"
                " WHERE id=? AND status='indexing'", (code, message, now_iso(), attempt_id))
            # 只把“当前这条构建中记录”标记为失败；已成功的索引记录不受影响。
            db.execute(
                "UPDATE embedding_indexes SET status='failed', error=? WHERE document_id=? AND id=? AND status='indexing'",
                (message, document_id, self._attempt_index_id(db, attempt_id)))

    def create_index_record(self, document_id: str, index_id: str, version_id: str,
                            model_signature: str) -> str:
        """独立候选索引；唯一约束以事务方式阻止同文档并发构建。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = now_iso()
            # 崩溃后的候选可在租约过期后重新建立，永远不失效化已发布索引。
            db.execute("UPDATE index_attempts SET status='failed', error_code='lease_expired', finished_at=? "
                       "WHERE status='indexing' AND index_id IN (SELECT id FROM embedding_indexes "
                       "WHERE document_id=? AND status='indexing' AND lease_expires_at<=?)",
                       (now, document_id, now))
            db.execute("UPDATE embedding_indexes SET status='failed', error='构建租约过期，可重新建立' "
                       "WHERE document_id=? AND status='indexing' AND lease_expires_at<=?", (document_id, now))
            db.execute(
                """INSERT INTO embedding_indexes(document_id, status, id, parse_version_id, model_signature,
                   source_signature_algo, created_at, is_active)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (document_id, "indexing", index_id, version_id, model_signature,
                 migrations.SIGNATURE_ALGO_VERSIONED, now_iso()))
            db.execute("UPDATE embedding_indexes SET lease_expires_at=? WHERE id=?",
                       ((datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(), index_id))
        return index_id

    def renew_index_lease(self, index_id: str) -> bool:
        """只有未过期的候选可以续租，过期执行者不能重新发布。"""
        with self.connect() as db:
            return db.execute("UPDATE embedding_indexes SET lease_expires_at=? WHERE id=? "
                              "AND status='indexing' AND lease_expires_at>?",
                              ((datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
                               index_id, now_iso())).rowcount == 1

    @staticmethod
    def _attempt_index_id(db: sqlite3.Connection, attempt_id: str) -> str | None:
        """读取构建尝试对应的索引 ID，用于只更新该条索引记录的状态。"""
        row = db.execute("SELECT index_id FROM index_attempts WHERE id=?", (attempt_id,)).fetchone()
        return row["index_id"] if row else None

    def index_vectors(self, index_id: str) -> list[tuple[str, str]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT chunk_id, vector FROM embedding_vectors_v2 WHERE index_id=? ORDER BY chunk_id",
                (index_id,)).fetchall()
        return [(row["chunk_id"], row["vector"]) for row in rows]

    def index_vector_map(self, index_id: str) -> dict[str, str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT chunk_id, vector FROM embedding_vectors_v2 WHERE index_id=?", (index_id,)).fetchall()
        return {row["chunk_id"]: row["vector"] for row in rows}

    def set_index_failed(self, document_id: str, message: str):
        """把构建中的索引记录标记为失败；已成功的索引记录不会被这条语句命中。"""
        with self.connect() as db:
            db.execute(
                "UPDATE embedding_indexes SET status='failed', error=? WHERE document_id=? AND status='indexing'",
                (message, document_id))
