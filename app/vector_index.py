"""单机文档向量索引：复用 SQLite 持久化，用精确余弦相似度检索。

当前针对单文档、小规模资料。规模扩大后可替换为专用向量数据库。
"""
import hashlib
import json

from app.embedding import APIEmbedding, EmbeddingError, normalize_vector
from app.repository import Repository
from app.schemas import Chunk


def source_signature(chunks: list[Chunk]) -> str:
    # 同时绑定块 ID、页码和内容，解析内容发生变化后不能继续使用旧向量。
    payload = [chunk.model_dump() for chunk in chunks]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class DocumentIndex:
    def __init__(self, repository: Repository, embedding: APIEmbedding):
        self.repository = repository
        self.embedding = embedding

    def status(self, document_id: str) -> dict:
        with self.repository.connect() as db:
            row = db.execute("SELECT * FROM embedding_indexes WHERE document_id=?", (document_id,)).fetchone()
        if not row:
            return {"status": "pending", "dimension": None, "chunk_count": 0, "error": None}
        result = dict(row)
        # stale 是计算得出的状态，不覆盖数据库中原有模型的索引记录。
        if result['status'] == 'indexed' and (
            result['provider_signature'] != self.embedding.signature or
            result['source_signature'] != source_signature(self.repository.chunks(document_id))
        ):
            result['status'] = 'stale'
        return {key: result[key] for key in ('status', 'dimension', 'chunk_count', 'error')}

    def build(self, document_id: str, rebuild: bool = False) -> dict:
        chunks = self.repository.chunks(document_id)
        if not chunks:
            raise EmbeddingError(409, "文档没有可索引分块，请先解析")
        if self.status(document_id)['status'] == 'indexed' and not rebuild:
            return self.status(document_id)  # 已有匹配索引时不重复调用收费接口。
        if not self.embedding.settings.embedding_api_key.strip():
            raise EmbeddingError(503, "请先配置 EMBEDDING_API_KEY 并重启服务")
        source = source_signature(chunks)
        with self.repository.connect() as db:
            # 原子领取文档索引任务，防止并发点击重复向量化。
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO embedding_indexes(document_id,status) VALUES (?, 'pending')", (document_id,))
            existing = db.execute("SELECT * FROM embedding_indexes WHERE document_id=?", (document_id,)).fetchone()
            # 另一个请求可能在首次状态检查后完成任务，此时直接复用，避免重复计费。
            if not rebuild and existing['status'] == 'indexed' and existing['provider_signature'] == self.embedding.signature and existing['source_signature'] == source:
                return {key: existing[key] for key in ('status', 'dimension', 'chunk_count', 'error')}
            claimed = db.execute("UPDATE embedding_indexes SET status='indexing', error=NULL WHERE document_id=? AND status!='indexing'", (document_id,))
            if claimed.rowcount != 1:
                raise EmbeddingError(409, "文档正在建立索引，请稍后刷新")
        try:
            vectors = self.embedding.embed([chunk.text for chunk in chunks])
            with self.repository.connect() as db:
                # 获取写锁后再次核对来源；所有向量和成功状态在同一事务内提交。
                db.execute('BEGIN IMMEDIATE')
                current = [Chunk(**dict(r)) for r in db.execute("SELECT * FROM chunks WHERE document_id=? ORDER BY rowid", (document_id,))]
                if source_signature(current) != source:
                    raise EmbeddingError(409, "文档分块已变化，请重新建立索引")
                db.execute("DELETE FROM embedding_vectors WHERE document_id=?", (document_id,))
                db.executemany("INSERT INTO embedding_vectors(document_id,chunk_id,vector) VALUES (?,?,?)",
                               [(document_id, chunk.id, json.dumps(vector)) for chunk, vector in zip(chunks, vectors)])
                db.execute("""UPDATE embedding_indexes SET status='indexed', provider_signature=?,
                    source_signature=?, dimension=?, chunk_count=?, error=NULL WHERE document_id=?""",
                    (self.embedding.signature, source, len(vectors[0]), len(chunks), document_id))
        except Exception as exc:
            # 失败批次不落盘；即使存在上次的向量，也通过 failed 状态阻止读取。
            message = str(exc) if isinstance(exc, EmbeddingError) else "索引保存失败，请检查本地数据库后重试"
            with self.repository.connect() as db:
                db.execute("UPDATE embedding_indexes SET status='failed', error=? WHERE document_id=?", (message, document_id))
            if isinstance(exc, EmbeddingError):
                raise
            raise EmbeddingError(500, message) from None
        return self.status(document_id)

    def search(self, document_id: str, query: str, top_k: int) -> list[dict]:
        if self.status(document_id)['status'] != 'indexed':
            raise EmbeddingError(409, "请先为当前模型和文档建立有效索引")
        query_vector = self.embedding.embed([query])[0]
        with self.repository.connect() as db:
            # 一次读事务取得索引元数据和向量，防止重建期间读到不一致的结果。
            db.execute('BEGIN')
            state = db.execute("SELECT * FROM embedding_indexes WHERE document_id=?", (document_id,)).fetchone()
            if not state or state['status'] != 'indexed' or state['provider_signature'] != self.embedding.signature:
                raise EmbeddingError(409, "索引状态已变化，请稍后重试")
            if state['dimension'] != len(query_vector):
                raise EmbeddingError(409, "查询向量维度与索引不一致，请重新建立索引")
            rows = db.execute("""SELECT c.*, v.vector FROM embedding_vectors v JOIN chunks c
                ON c.id=v.chunk_id AND c.document_id=v.document_id WHERE v.document_id=? ORDER BY c.rowid""", (document_id,)).fetchall()
        if len(rows) != state['chunk_count']:
            raise EmbeddingError(409, "索引分块不完整，请重新建立索引")
        current = [Chunk(id=r['id'], document_id=document_id, page=r['page'], text=r['text']) for r in rows]
        if source_signature(current) != state['source_signature']:
            raise EmbeddingError(409, "原文已变化，请重新建立索引")
        results = []
        try:
            for row in rows:
                vector = normalize_vector(json.loads(row['vector']))
                if len(vector) != len(query_vector):
                    raise ValueError('dimension mismatch')
                score = max(-1.0, min(1.0, sum(a * b for a, b in zip(query_vector, vector))))
                results.append({"chunk_id": row['id'], "document_id": document_id,
                                "page": row['page'], "text": row['text'], "score": score})
        except (ValueError, TypeError, EmbeddingError):
            raise EmbeddingError(409, "索引向量损坏，请重新建立索引") from None
        # 相似度表示排序分值，不代表回答正确概率；此接口只检索原文，不生成答案。
        return sorted(results, key=lambda item: item['score'], reverse=True)[:top_k]
