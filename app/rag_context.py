"""RAG 证据构建：固定版本快照、候选去重、字符预算与引用编号分配（工程包 B）。

设计边界（对应任务书第 5 节）：

1. 一次提问只使用一次检索结果；证据块的 document_id 与 parse_version_id 必须与
   本次快照一致，混入其他文档或版本直接受控失败，而不是默默拼接。
2. 引用编号由服务端对**最终入选证据**按整块装入顺序分配；只在候选里、
   因预算未送入模型的块不会获得编号，模型也无法引用它。
3. 采用确定性整块装入：块连同必要元数据序列化后计入预算，容纳不下就跳过并
   继续尝试后续候选；入选块绝不会被从尾部硬截断，标题、表头、条件、脚注
   不会为了凑字数被删掉。因预算舍弃任何块时标记 truncated=True
   （表示证据集合不完整，不表示引用文本被截断）。
4. 字符预算不等于 token 预算；这里只做本地输入限流，真正的模型上下文限制
   仍需供应商实测，因此不凭空引入与 DeepSeek 不匹配的 tokenizer。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings
from app.schemas import QualityWarning, SourceLocation

# 证据包序列化格式版本：变更字段含义时必须同步提升，便于排查历史记录。
EVIDENCE_SCHEMA_VERSION = "rag-evidence-v1"


class RagError(Exception):
    """RAG 业务错误：只携带面向用户的安全提示与稳定错误码。

    上游响应正文、异常堆栈、密钥与完整 Prompt 一律不进入这里，
    避免通过 HTTP 响应、页面或日志泄露。
    """

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def normalize_text(value: str) -> str:
    """统一 CRLF 为 LF 并去掉首尾空白；不改大小写、不改数字、不删标点。

    这是唯一允许的文本规范化：任何更强的“清洗”都会让引述校验失去意义。
    """
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _strip_controls(value: str) -> str:
    """去掉除制表符与换行外的控制字符，避免把不可见字符塞进提示词。"""
    return "".join(ch for ch in value if ch >= " " or ch in "\t\n")


@dataclass
class EvidenceItem:
    """单条证据：绑定唯一文档与唯一解析版本，并保留完整来源。"""
    reference_id: int
    chunk_id: str
    document_id: str
    parse_version_id: str | None
    page: int | None
    text: str
    sources: list[SourceLocation] = field(default_factory=list)
    chunk_type: str | None = None
    heading_path: str | None = None
    order_index: int | None = None
    block_id: str | None = None
    score: float = 0.0

    def payload(self) -> dict[str, Any]:
        """序列化为送入模型的数据对象；只包含可作为事实依据的字段。

        不包含文档 ID、chunk ID、文件路径或服务端路径：模型看不到内部标识，
        也就不可能伪造它们；引用卡片上的定位信息全部由后端从本次证据映射复制。
        """
        source_labels = [source.label() for source in self.sources]
        entry: dict[str, Any] = {
            "ref": self.reference_id,
            "content": self.text,
        }
        if self.heading_path:
            entry["heading"] = self.heading_path
        if self.chunk_type:
            entry["block_type"] = self.chunk_type
        if source_labels:
            entry["locations"] = source_labels
        return entry


@dataclass
class EvidencePack:
    """本次请求的证据包：入选证据、被舍弃的候选与固定版本信息。"""
    items: list[EvidenceItem]
    candidates: list[EvidenceItem]
    index_id: str | None
    parse_version_id: str | None
    truncated: bool
    context_chars: int
    drop_reasons: list[str] = field(default_factory=list)
    rejected: list[tuple[EvidenceItem, str]] = field(default_factory=list)

    def item_by_ref(self, reference_id: int) -> EvidenceItem | None:
        for item in self.items:
            if item.reference_id == reference_id:
                return item
        return None

    def refs(self) -> set[int]:
        return {item.reference_id for item in self.items}

    def payload(self) -> list[dict[str, Any]]:
        return [item.payload() for item in self.items]

    def serialized(self) -> str:
        """参考资料序列化结果；预算与超限检查都以它的实际字符数为准。"""
        return json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":"))


def _candidate_from_hit(hit: dict) -> EvidenceItem:
    """把 DocumentIndex.search 的命中转换为证据候选；来源结构完整保留。"""
    sources: list[SourceLocation] = []
    for raw in hit.get("sources") or []:
        if not isinstance(raw, dict):
            continue
        try:
            sources.append(SourceLocation(**raw))
        except (TypeError, ValueError):
            # 单个来源损坏不影响其余来源，但绝不能伪造页码顶替。
            continue
    score = hit.get("score")
    return EvidenceItem(
        reference_id=0,
        chunk_id=str(hit.get("chunk_id") or ""),
        document_id=str(hit.get("document_id") or ""),
        parse_version_id=hit.get("parse_version_id"),
        page=hit.get("page"),
        text=str(hit.get("text") or "").replace("\r\n", "\n"),
        sources=sources,
        chunk_type=hit.get("chunk_type"),
        heading_path=hit.get("heading_path"),
        order_index=hit.get("order_index"),
        block_id=hit.get("block_id"),
        score=float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0,
    )


def build_candidates(results: list[dict], *, min_score: float | None) -> tuple[list[EvidenceItem], list[str]]:
    """按分值排序、按 chunk_id 去重并应用可选阈值，返回候选与舍弃原因。

    规则：
    - 排序键为 (-score, order_index, chunk_id)，同分时顺序稳定，便于复现；
    - 同一 chunk_id 只保留一条；
    - 归一化文本完全相同的重复片段只保留分值最高的一条（同一内容不重复占预算）；
    - 相似度只是排序分值，不是置信度；未配置阈值时不做任何过滤。

    注意：这里**只做去重**，不判断归属。跨文档/跨版本的检查发生在
    build_evidence 中，并且在该检查之前完成；否则跨文档的重复文本会被
    当成“重复片段”提前丢弃，从而掩盖真正的范围错误。
    """
    candidates: list[EvidenceItem] = []
    seen_ids: set[str] = set()
    seen_text: set[tuple] = set()
    drop_reasons: list[str] = []

    # 先排序再去重，避免保留输入顺序中分数较低的重复项。
    ordered = sorted((h for h in results if isinstance(h, dict)),
                     key=lambda h: (-float(h.get('score') or 0),
                                    h.get('order_index') if h.get('order_index') is not None else 1 << 30,
                                    str(h.get('chunk_id') or '')))
    for hit in ordered:
        if not isinstance(hit, dict):
            continue
        item = _candidate_from_hit(hit)
        if not item.chunk_id:
            drop_reasons.append("候选缺少 chunk_id，已丢弃")
            continue
        if item.chunk_id in seen_ids:
            drop_reasons.append(f"重复候选 chunk_id={item.chunk_id}，已去重")
            continue
        seen_ids.add(item.chunk_id)
        if min_score is not None and item.score < min_score:
            drop_reasons.append(
                f"候选 chunk_id={item.chunk_id} 分值低于配置阈值，已丢弃")
            continue
        if not item.text.strip():
            drop_reasons.append(f"候选 chunk_id={item.chunk_id} 没有正文，已丢弃")
            continue
        # 同一句话出现在不同章节/表格/页上可能支持不同事实，不能只按正文消重。
        key = (item.text, item.heading_path, item.block_id, item.page,
               json.dumps([s.model_dump() for s in item.sources], sort_keys=True))
        if key in seen_text:
            drop_reasons.append(f"候选 chunk_id={item.chunk_id} 与已选片段正文重复，已去重")
            continue
        seen_text.add(key)
        candidates.append(item)

    candidates.sort(key=lambda entry: (-entry.score,
                                       entry.order_index if entry.order_index is not None else 1 << 30,
                                       entry.chunk_id))
    return candidates, drop_reasons


def detect_scope_mismatch(candidates: list[EvidenceItem], *, document_id: str | None,
                          parse_version_id: str | None) -> str | None:
    """检查候选是否混入其他文档或其他解析版本。

    必须在任何“未入选/去重”过滤**之前**对全部候选执行，
    这样跨文档同词片段、跨版本块都会以受控失败暴露，而不是被静默丢弃。
    """
    for candidate in candidates:
        if document_id and candidate.document_id != document_id:
            return "检索结果混入了其他文档的分块，已拒绝本次回答"
        if candidate.parse_version_id != parse_version_id:
            return "检索结果混入了其他解析版本的分块，已拒绝本次回答"
    return None


def pack_evidence(candidates: list[EvidenceItem], *, context_max_chars: int,
                  serialize: Callable[[list[EvidenceItem]], str],
                  max_items: int | None = None) -> tuple[list[EvidenceItem], bool, int, list[str]]:
    """确定性整块装入：按顺序尝试放入，放不下就跳过并继续尝试后续候选。

    返回 (入选证据, 是否舍弃过块, 序列化字符数, 舍弃原因)。
    绝不截断入选块正文，因此不会为了凑预算删掉表头、条件或脚注。
    """
    selected: list[EvidenceItem] = []
    drop_reasons: list[str] = []
    truncated = False
    context_chars = 0

    for candidate in candidates:
        if max_items is not None and len(selected) >= max_items:
            drop_reasons.append(f"候选 chunk_id={candidate.chunk_id} 超出证据块数上限，已舍弃")
            continue
        # 先用占位编号序列化，确认整块装入后是否仍在预算内。
        candidate.reference_id = len(selected) + 1
        serialized = serialize(selected + [candidate])
        if len(serialized) > context_max_chars:
            candidate.reference_id = 0
            truncated = True
            drop_reasons.append(
                f"候选 chunk_id={candidate.chunk_id} 超出参考资料字符预算，已整块舍弃")
            continue
        selected.append(candidate)
        context_chars = len(serialized)

    # 重新编号，保证入选证据的引用编号严格连续、从 1 开始。
    for position, item in enumerate(selected, start=1):
        item.reference_id = position
    if selected:
        context_chars = len(serialize(selected))
    return selected, truncated, context_chars, drop_reasons


def _relevant_warnings(warnings: list[QualityWarning], items: list[EvidenceItem]) -> list[QualityWarning]:
    """保留与本次证据相关的质量告警：公式缓存缺失、OCR 限制等不得被静默丢弃。"""
    if not warnings:
        return []
    if not items:
        return []
    block_ids = {item.block_id for item in items if item.block_id}
    pages = {p for item in items for p in [item.page, *[s.page for s in item.sources]] if p is not None}
    nodes = {s.node_ref for item in items for s in item.sources if s.node_ref}
    sheets = {s.sheet_name for item in items for s in item.sources if s.sheet_name}
    keep: list[QualityWarning] = []
    for warning in warnings:
        if warning.block_id and warning.block_id in block_ids:
            keep.append(warning)
            continue
        if warning.page is not None and warning.page in pages:
            keep.append(warning)
            continue
        if warning.scope == "document":
            # 整篇级限制（例如 OCR 语言、公式识别）继续对本次回答生效。
            keep.append(warning)
            continue
        if (warning.source and warning.source.node_ref in nodes) or (warning.sheet_name and warning.sheet_name in sheets):
            keep.append(warning)
    return keep


@dataclass
class EvidenceBuildResult:
    """证据构建结果：证据包 + 供响应与页面展示的告警与限制说明。"""
    pack: EvidencePack
    warnings: list[QualityWarning]
    limitations: list[str]
    fatal_reason: str | None = None


def build_evidence(search_result: dict, settings: Settings, *,
                   version_warnings: list[QualityWarning] | None = None,
                   context_cap: int | None = None,
                   expected_document_id: str | None = None) -> EvidenceBuildResult:
    """由一次检索结果构建本次证据包，并完成版本快照校验。

    - context_cap 由提示词实际序列化长度反推（见 app.rag），保证最终消息体不超限；
    - 证据混入其他文档或版本时返回 fatal_reason，由服务层转换成为受控错误；
    - 质量状态为 invalid 的版本不能作为证据。
    """
    document_id = expected_document_id or search_result.get("document_id")
    index_id = search_result.get("index_id")
    parse_version_id = search_result.get("parse_version_id")
    raw_results = search_result.get("results") or []
    if not isinstance(raw_results, list):
        raw_results = []

    candidates, drop_reasons = build_candidates(raw_results, min_score=settings.rag_min_score)
    # 先在**全部候选**上做归属检查：跨文档或跨版本时受控失败，
    # 而不是让“去重/预算”把这些块悄悄过滤掉。
    all_items = [_candidate_from_hit(item) for item in raw_results if isinstance(item, dict)]
    fatal_reason = detect_scope_mismatch(all_items, document_id=document_id,
                                        parse_version_id=parse_version_id)

    if fatal_reason:
        # 受控失败：不组装 Prompt、不调用生成，也不把拼接结果当成答案。
        pack = EvidencePack(items=[], candidates=[], index_id=index_id,
                            parse_version_id=parse_version_id, truncated=False,
                            context_chars=0, drop_reasons=drop_reasons)
        return EvidenceBuildResult(pack=pack, warnings=[], limitations=[],
                                   fatal_reason=fatal_reason)

    max_chars = settings.rag_context_max_chars if context_cap is None else min(
        settings.rag_context_max_chars, context_cap)
    max_chars = max(max_chars, 0)

    def serialize(items: list[EvidenceItem]) -> str:
        return json.dumps([item.payload() for item in items],
                          ensure_ascii=False, separators=(",", ":"))

    selected, truncated, context_chars, budget_reasons = pack_evidence(
        candidates, context_max_chars=max_chars, serialize=serialize,
        max_items=settings.rag_context_k)
    drop_reasons.extend(budget_reasons)
    # truncated 只表示“因为字符预算不足而舍弃了本可入选的整块”，
    # 不表示被 top_k 上限截断（那是检索条数上限，不是预算问题）。

    pack = EvidencePack(items=selected, candidates=candidates, index_id=index_id,
                        parse_version_id=parse_version_id, truncated=truncated,
                        context_chars=context_chars, drop_reasons=drop_reasons)

    warnings = _relevant_warnings(version_warnings or [], selected)
    limitations: list[str] = []
    if truncated:
        limitations.append(
            "本次回答只使用了字符预算内的部分检索片段，证据集合不完整；"
            "如需覆盖更多原文，请缩小问题范围或调大上下文预算后重试")
    if selected and all(not item.sources for item in selected):
        limitations.append("所选片段没有来源定位记录，引用卡片只能显示已有定位信息，未补造页码")
    if pack.parse_version_id and (search_result.get("is_legacy") or False):
        limitations.append("本次证据来自旧库迁移的兼容索引，定位信息有限")
    return EvidenceBuildResult(pack=pack, warnings=warnings, limitations=limitations)


def find_unselected_hits(search_result: dict) -> set[str]:
    """候选中存在但未获得引用编号的 chunk_id，用于错误定位与测试断言。"""
    return {str(hit.get("chunk_id")) for hit in (search_result.get("results") or [])
            if isinstance(hit, dict)}


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "EvidenceBuildResult",
    "EvidenceItem",
    "EvidencePack",
    "RagError",
    "build_candidates",
    "build_evidence",
    "detect_scope_mismatch",
    "find_unselected_hits",
    "normalize_text",
    "pack_evidence",
]
