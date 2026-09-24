"""版本化文档向量索引：索引绑定唯一解析版本，检索只读取该版本的分块与向量。

对应任务书工程包 E 的索引要求：
- 保留现有向量校验、批次顺序、维度检查和模型不匹配处理；
- 构建前固定解析版本；向量先进入候选索引，全部成功并核对状态后原子切换；
  失败不修改已有成功索引的可用状态；
- 建 B 索引期间若 C 成为活动解析版本，B 的构建在发布前核对目标版本，
  变化时标记 superseded 并保留原活动索引；
- 同一版本同一模型重复建立时复用，不重复调用收费接口；
- 查询在一致快照中读取索引、对应版本分块和向量；切换过程中只能得到完整旧版或完整新版；
- source_signature 有算法版本：新算法绑定解析版本与块结构，旧算法保留用于验证旧索引。
"""
import hashlib
import json
import logging
import sqlite3
from uuid import uuid4
from threading import Event, Thread

from app import migrations
from app.embedding import APIEmbedding, EmbeddingError, normalize_vector
from app.repository import Repository
from app.schemas import Chunk

logger = logging.getLogger(__name__)

SIGNATURE_ALGO_LEGACY = migrations.SIGNATURE_ALGO_LEGACY
SIGNATURE_ALGO_VERSIONED = migrations.SIGNATURE_ALGO_VERSIONED

# 允许的索引状态；stale 是计算得出的状态，不写回数据库。
_STATUS_LABELS = {
    "pending": "未建立索引",
    "indexing": "正在建立索引",
    "indexed": "索引可用",
    "failed": "索引失败",
    "stale": "模型或原文已变更，请重建索引",
}


def source_signature_legacy(chunks: list[Chunk]) -> str:
    """旧签名算法：sha256(json(chunk.model_dump()))。

    旧库中的索引使用该算法；保留它用于验证旧索引，避免因新增字段导致
    旧索引被误判为 stale，也避免重算签名掩盖真实损坏。
    """
    payload = []
    for chunk in chunks:
        payload.append({
            "id": chunk.id,
            "document_id": chunk.document_id,
            "page": chunk.page,
            "text": chunk.text,
        })
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_signature_versioned(version_id: str | None, chunks: list[Chunk]) -> str:
    """新签名算法：绑定解析版本、块 ID、显式顺序、块类型与文本。

    只使用索引实际依赖的字段，避免预览展示字段变化导致索引失效；
    解析版本 ID 唯一标识一次解析，因此不同版本的签名必然不同。
    """
    payload = {
        "algo": SIGNATURE_ALGO_VERSIONED,
        "parse_version_id": version_id,
        "chunks": [
            {
                "id": chunk.id,
                "order": chunk.order_index,
                "type": chunk.chunk_type,
                "block": chunk.block_id,
                "text": chunk.text,
            }
            for chunk in sorted(chunks, key=lambda item: (item.order_index is None, item.order_index))
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_signature(chunks: list[Chunk], *, version_id: str | None = None,
                     algo: str | None = None) -> str:
    """按算法版本计算来源签名；未指定算法时使用新算法。"""
    if algo == SIGNATURE_ALGO_LEGACY:
        return source_signature_legacy(chunks)
    return source_signature_versioned(version_id, chunks)


def compute_signature(chunks: list[Chunk], version_id: str | None, algo: str | None) -> str:
    """兼容旧调用名：按算法版本计算签名。"""
    return source_signature(chunks, version_id=version_id, algo=algo)


class DocumentIndex:
    """单文档向量索引；复用 SQLite 持久化，用精确余弦相似度检索。"""

    def __init__(self, repository: Repository, embedding: APIEmbedding):
        self.repository = repository
        self.embedding = embedding

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self, document_id: str) -> dict:
        """返回索引状态字典（兼容旧响应字段），并补充版本信息。"""
        info = self.index_info(document_id)
        return {
            "status": info["status"],
            "dimension": info["dimension"],
            "chunk_count": info["chunk_count"],
            "error": info["error"],
            "index_id": info["index_id"],
            "parse_version_id": info["parse_version_id"],
            "matches_active_version": info["matches_active_version"],
            "is_legacy": info["is_legacy"],
            "attempts": info["attempts"],
        }

    def index_info(self, document_id: str) -> dict:
        """计算索引状态：数据库记录 + 模型签名 + 来源签名三方核对。"""
        active = self.repository.active_index(document_id)
        row = self.repository.index_row(document_id)
        if active is None:
            if row is not None and row.status in {"indexing", "failed"}:
                return {
                    "status": row.status, "dimension": row.dimension, "chunk_count": row.chunk_count,
                    "error": row.error, "index_id": row.id, "parse_version_id": row.parse_version_id,
                    "matches_active_version": False, "is_legacy": row.is_legacy,
                    "attempts": [attempt.model_dump() for attempt in row.attempts],
                    "message": _STATUS_LABELS.get(row.status, ""),
                }
            return {"status": "pending", "dimension": None, "chunk_count": 0, "error": None,
                    "index_id": None, "parse_version_id": None, "matches_active_version": False,
                    "is_legacy": False, "attempts": [], "message": _STATUS_LABELS["pending"]}

        version_id = active.parse_version_id
        chunks = self.repository.chunks(document_id, version_id)
        algo = active.source_signature_algo or SIGNATURE_ALGO_VERSIONED
        signature = compute_signature(chunks, version_id, algo)
        stale_reason = None
        if active.model_signature != self.embedding.signature:
            # 提供方配置变化后旧索引按原规则判为不兼容，不强行使用不同模型的向量。
            stale_reason = "Embedding 模型或网关已变更，请重建索引"
        elif active.source_signature != signature:
            stale_reason = "解析内容与索引记录不一致，请重建索引"
        elif active.chunk_count != len(chunks):
            stale_reason = "索引分块数量与解析版本不一致，请重建索引"
        status = "stale" if stale_reason else "indexed"
        return {
            "status": status, "dimension": active.dimension,
            "chunk_count": active.chunk_count, "error": stale_reason or active.error,
            "index_id": active.id, "parse_version_id": version_id,
            "matches_active_version": active.matches_active_version,
            "is_legacy": active.is_legacy,
            "attempts": [attempt.model_dump() for attempt in active.attempts],
            "message": _STATUS_LABELS[status],
        }

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self, document_id: str, rebuild: bool = False, *, version_id: str | None = None) -> dict:
        """为指定解析版本建立索引；默认使用当前活动解析版本。

        流程：固定目标版本 → 登记尝试 → 候选向量 → 发布前核对版本 → 原子切换。
        任何失败都不修改已有成功索引的可用状态。
        """
        document = self.repository.get(document_id)
        if document is None:
            raise EmbeddingError(404, "文档不存在")
        target_version = version_id or document.active_parse_version_id
        if not target_version:
            raise EmbeddingError(409, "文档尚未成功解析，请先解析后再建立索引")
        version = self.repository.get_parse_version(target_version)
        if version is None or version.document_id != document_id:
            raise EmbeddingError(404, "解析版本不存在或不属于该文档")
        chunks = self.repository.chunks(document_id, target_version)
        if not chunks:
            # 不创建空的“成功索引”。
            raise EmbeddingError(409, "该解析版本没有可索引分块；若整份文档为空，请先确认原件内容")
        if not self.embedding.settings.embedding_api_key.strip():
            raise EmbeddingError(503, "请先配置 EMBEDDING_API_KEY 并重启服务")

        current = self.index_info(document_id)
        if (not rebuild and current["status"] == "indexed"
                and current["parse_version_id"] == target_version
                and current["matches_active_version"]):
            # 同一版本同一模型已建立：直接复用，不重复调用收费接口。
            return self.status(document_id)

        signature = compute_signature(chunks, target_version, SIGNATURE_ALGO_VERSIONED)
        candidate_id = f"idx-{uuid4().hex}"
        # 先登记索引记录（index_attempts.index_id 有外键约束），再登记本次构建尝试。
        # 已有索引记录时沿用其 ID，避免破坏既有可用索引与向量外键。
        try:
            index_id = self.repository.create_index_record(document_id, candidate_id, target_version,
                                                          self.embedding.signature)
        except sqlite3.IntegrityError:
            raise EmbeddingError(409, "文档已有索引构建任务，请稍后刷新") from None
        attempt_id = self.repository.begin_index_attempt(
            document_id, index_id, target_version, self.embedding.signature)
        done = Event()

        def heartbeat():
            while not done.wait(30):
                try:
                    if not self.repository.renew_index_lease(index_id):
                        return
                except Exception:
                    logger.exception("候选索引续租失败，发布前将重新校验")
                    return

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            vectors = self.embedding.embed([chunk.text for chunk in chunks])
            if len(vectors) != len(chunks):
                raise EmbeddingError(502, "Embedding 返回的向量数量与分块数量不一致")
            dimension = len(vectors[0])
            # 向量先进入候选索引；全部成功后核对目标版本再原子切换。
            published = self.repository.activate_index(
                document_id, index_id, attempt_id, version_id=target_version,
                model_signature=self.embedding.signature, source_signature=signature,
                source_signature_algo=SIGNATURE_ALGO_VERSIONED, dimension=dimension,
                chunk_count=len(chunks),
                vectors=[(chunk.id, json.dumps(vector), dimension)
                         for chunk, vector in zip(chunks, vectors)],
                chunking_algo=chunks[0].chunk_type or "structured",
                expected_version_id=target_version)
            if not published:
                raise EmbeddingError(
                    409, "构建期间该文档的活动解析版本已变化，本次索引构建已过期；"
                         "原有索引保持可用，请对新版本重新建立索引")
        except Exception as exc:
            message = str(exc) if isinstance(exc, EmbeddingError) else "索引构建失败，请检查服务日志后重试"
            code = "embedding_failed" if isinstance(exc, EmbeddingError) else "index_build_failed"
            self.repository.fail_index_attempt(attempt_id, code=code, message=message,
                                              document_id=document_id)
            if isinstance(exc, EmbeddingError):
                raise
            logger.exception("索引构建失败：文档 %s", document_id)
            raise EmbeddingError(500, message) from None
        finally:
            done.set()
            thread.join(timeout=2)
        return self.status(document_id)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(self, document_id: str, query: str, top_k: int) -> dict:
        """使用当前可用索引检索，返回命中、来源与版本信息。

        返回结构包含 index_id、parse_version_id、is_stale 与每条来源，
        页面据此明确标注“检索仍使用旧版本”。
        """
        info = self.index_info(document_id)
        if info["status"] != "indexed":
            if info["status"] == "stale":
                raise EmbeddingError(409, f"{info['error'] or '索引已过期'}，请重新建立索引")
            raise EmbeddingError(409, "请先为当前模型和文档建立有效索引")
        query_vector = self.embedding.embed([query])[0]
        version_id = info["parse_version_id"]
        index_id = info["index_id"]
        # 一次读事务取得索引状态、对应版本分块与向量，防止切换期间读到不一致结果。
        chunks = self.repository.chunks(document_id, version_id)
        with self.repository.connect() as db:
            db.execute("BEGIN")
            state = db.execute(
                "SELECT * FROM embedding_indexes WHERE document_id=? AND id=? AND status='indexed'",
                (document_id, index_id)).fetchone()
            if state is None or state["id"] != index_id:
                raise EmbeddingError(409, "索引状态已变化，请稍后重试")
            if state["provider_signature"] != self.embedding.signature:
                raise EmbeddingError(409, "索引模型签名已变化，请重新建立索引")
            if state["dimension"] != len(query_vector):
                raise EmbeddingError(409, "查询向量维度与索引不一致，请重新建立索引")
            vector_rows = db.execute(
                "SELECT chunk_id, vector FROM embedding_vectors_v2 WHERE index_id=?",
                (index_id,)).fetchall()
        vector_map = {row["chunk_id"]: row["vector"] for row in vector_rows}
        if len(vector_map) != len(chunks) or any(chunk.id not in vector_map for chunk in chunks):
            raise EmbeddingError(409, "索引分块不完整，请重新建立索引")
        # 复核来源签名：确保向量与当前版本分块一一对应，绝不混用版本。
        algo = state["source_signature_algo"] or SIGNATURE_ALGO_VERSIONED
        if compute_signature(chunks, version_id, algo) != state["source_signature"]:
            raise EmbeddingError(409, "原文已变化，请重新建立索引")
        results = []
        try:
            for chunk in chunks:
                # 向量损坏（空向量、非数值、维度不符）一律拒绝检索，不给出无意义的排序结果。
                try:
                    vector = normalize_vector(json.loads(vector_map[chunk.id]))
                except EmbeddingError:
                    raise ValueError("corrupt vector") from None
                if len(vector) != len(query_vector):
                    raise ValueError("dimension mismatch")
                score = max(-1.0, min(1.0, sum(a * b for a, b in zip(query_vector, vector))))
                results.append({
                    "chunk_id": chunk.id, "document_id": document_id,
                    "page": chunk.page, "text": chunk.text, "score": score,
                    "parse_version_id": version_id, "chunk_type": chunk.chunk_type,
                    "heading_path": chunk.heading_path,
                    "sources": [source.model_dump() for source in chunk.sources],
                })
        except (ValueError, TypeError, EmbeddingError):
            raise EmbeddingError(409, "索引向量损坏，请重新建立索引") from None
        # 相似度表示排序分值，不代表回答正确概率；此接口只检索原文，不生成答案。
        results.sort(key=lambda item: item["score"], reverse=True)
        active_version = self.repository.get(document_id)
        is_old_version = bool(active_version and active_version.active_parse_version_id
                              and active_version.active_parse_version_id != version_id)
        return {
            "index_id": index_id, "parse_version_id": version_id,
            "is_old_version": is_old_version, "is_legacy": info["is_legacy"],
            "chunk_count": len(chunks),
            "message": ("检索结果来自历史解析版本，当前预览已更新；"
                        "如需检索最新版本请重建索引") if is_old_version else "检索结果来自当前预览版本",
            "results": results[:top_k],
        }
