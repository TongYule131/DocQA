# SQLite 持久化层：封装文档状态及文本分块的读写。
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.schemas import Chunk, Document


class Repository:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        # 每次操作独立连接；正常退出提交事务，发生异常回滚，最后关闭连接。
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        # 建表和索引可重复执行；文件原件独立保存在 uploads 目录。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
                    created_at TEXT NOT NULL, status TEXT NOT NULL,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    page INTEGER NOT NULL, text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);
            """)
            # 框架使用同步解析；上次进程被中断的任务允许用户重新解析。
            db.execute("UPDATE documents SET status='failed', error='解析被中断，请重试' WHERE status='parsing'")

    def create(self, document: Document):
        # 参数绑定将数据与 SQL 分开；这里只保存元数据，不保存文件内容。
        values = document.model_dump()
        with self.connect() as db:
            db.execute(
                "INSERT INTO documents VALUES (:id,:filename,:size,:created_at,:status,:page_count,:chunk_count,:error)",
                values,
            )

    def list_documents(self) -> list[Document]:
        # 按上传时间倒序返回，工作台优先展示最新资料。
        with self.connect() as db:
            return [Document(**dict(row)) for row in db.execute("SELECT * FROM documents ORDER BY created_at DESC")]

    def get(self, document_id: str) -> Document | None:
        # 存储层用 None 表示不存在，由接口层决定 HTTP 响应。
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        return Document(**dict(row)) if row else None

    def claim_parse(self, document_id: str) -> bool:
        # 通过条件更新原子地领取任务，避免并发请求重复解析同一份文档。
        with self.connect() as db:
            result = db.execute("UPDATE documents SET status='parsing', error=NULL WHERE id=? AND status IN ('uploaded','failed')", (document_id,))
            return result.rowcount == 1

    def finish_parse(self, document_id: str, pages: int, chunks: list[Chunk]):
        # 替换分块与更新文档状态处于同一事务，避免只写入部分结果。
        with self.connect() as db:
            db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            db.executemany("INSERT INTO chunks VALUES (:id,:document_id,:page,:text)", [c.model_dump() for c in chunks])
            db.execute("UPDATE documents SET status='parsed', page_count=?, chunk_count=?, error=NULL WHERE id=?", (pages, len(chunks), document_id))

    def fail_parse(self, document_id: str, error: str):
        # 保存可展示的错误信息，failed 状态允许重新领取解析任务。
        with self.connect() as db:
            db.execute("UPDATE documents SET status='failed', error=? WHERE id=?", (error, document_id))

    def chunks(self, document_id: str) -> list[Chunk]:
        # rowid 按写入顺序排列，保持解析时的页序及页内分块顺序。
        with self.connect() as db:
            return [Chunk(**dict(row)) for row in db.execute("SELECT * FROM chunks WHERE document_id=? ORDER BY rowid", (document_id,))]
