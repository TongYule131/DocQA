"""文档摘要：分批生成、汇总与原文引用链。

本模块是**生产代码路径**，worker、脚本与测试都走同一份实现。

关键不变量（任务书 §7）：

1. 小文档一次完成；长文档按确定批次生成已校验中间结果，再汇总。
2. 中间摘要是模型派生内容，**不是原文**：汇总阶段只接收已校验条目与引述，
   最终事实引用仍必须回落到原文连续子串——本模块在汇总后把所有 `refs`
   沿“中间条目 → 批次单元编号 → 原文单元”的链解析成结构化引用。
3. 汇集阶段禁止输出 `quotes`：模型不能用自己写的“原文”给最终摘要背书。
4. 任一必需批次未完成、校验失败或预算耗尽时，不发布“完整全文摘要”；
   已校验检查点保留供明确重试。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from app.analysis_prompts import (
    SUMMARY_PROMPT_VERSION,
    SUMMARY_PROTOCOL_VERSION,
    SUMMARY_REDUCE_BATCH_MAX_CHARS,
    build_batch_user_prompt,
    build_reduce_user_prompt,
    summary_reduce_system_prompt,
    summary_system_prompt,
)
from app.analysis_sources import InputUnit
from app.analysis_validation import (
    AnalysisOutputError,
    ValidatedReduce,
    ValidatedSummaryBatch,
    ValidatedSummaryPoint,
    validate_reduce,
    validate_summary_batch,
)

PROMPT_VERSION = SUMMARY_PROMPT_VERSION
PROTOCOL_VERSION = SUMMARY_PROTOCOL_VERSION

@dataclass
class SummaryBatchRequest:
    system_prompt: str
    user_prompt: str
    units: list[InputUnit]
    batch_index: int


@dataclass
class SummaryReduceRequest:
    system_prompt: str
    user_prompt: str
    entries: list[dict]


def build_batch_request(*, units: list[InputUnit], document_name: str, version_id: str,
                        batch_index: int, batch_total: int) -> SummaryBatchRequest:
    """构造一批摘要请求；编号在本批内从 1 连续分配。"""
    for position, unit in enumerate(units, start=1):
        if unit.batch_local_id != position:
            raise ValueError("批次内单元编号必须从 1 连续分配")
    return SummaryBatchRequest(
        system_prompt=summary_system_prompt(),
        user_prompt=build_batch_user_prompt(
            kind="summary", document_name=document_name, version_id=version_id,
            units=[unit.payload() for unit in units], batch_index=batch_index,
            batch_total=batch_total),
        units=units, batch_index=batch_index)


def validate_batch(raw: str, *, units: list[InputUnit], batch_id: str) -> ValidatedSummaryBatch:
    return validate_summary_batch(raw, units=units, batch_id=batch_id)


def batch_entries(batch_index: int, batch_id: str, batch: ValidatedSummaryBatch,
                  resolve_quote) -> list[dict]:
    """把一批已校验摘要展开为汇总输入条目。

    每条条目携带：稳定 `item_id`、文本、以及由服务端从批内引述解析出的原文引用。
    `item_id` 是服务端给定的引用凭据；汇总模型只能引用收到的编号，不能自造。

    `item_id` 形如 `b1-p1`（批内第 1 条要点）与 `b1-e1`（批内第 1 条例外）：
    要点与例外各自独立编号，便于人工核对，也避免两类条目共用序号造成指代歧义。
    """
    entries: list[dict] = []
    # 每个分区各自计数：p 与 e 的序号互不影响。
    counters = {"p": 0, "e": 0}

    def add(kind: str, point: ValidatedSummaryPoint, section: str) -> None:
        counters[section] += 1
        resolved = [resolve_quote(batch_index, ref) for ref in point.refs]
        entries.append({
            "item_id": f"{batch_id}-{section}{counters[section]}",
            "kind": kind,
            "text": point.text,
            "batch_id": batch_id,
            "refs": list(point.refs),
            # 原文引用只保留定位字段：批内编号仅用于合并去重。
            "original_refs": [{key: value for key, value in entry.items() if key != "ref"}
                              for entry in resolved],
        })

    for point in batch.main_points:
        add("point", point, "p")
    for point in batch.exceptions:
        add("exception", point, "e")
    if not entries:
        # 一批没有任何可引用要点：仍然登记主题概述，避免汇总阶段完全看不到该批次。
        entries.append({
            "item_id": f"{batch_id}-overview",
            "kind": "overview",
            "text": batch.topic_overview,
            "batch_id": batch_id,
            "refs": [],
            "original_refs": [],
        })
    return entries


def build_reduce_request(*, document_name: str, version_id: str, batch_total: int,
                         entries: list[dict]) -> SummaryReduceRequest:
    """发送每条引用的完整原文，不把预览或派生摘要冒充模型已见的依据。"""
    payload_entries = [{
        "item_id": entry["item_id"],
        "kind": entry["kind"],
        "text": entry["text"],
        "batch_id": entry["batch_id"],
        "original_quotes": [original["quote"] for original in entry["original_refs"]],
    } for entry in entries]
    # 与规划预留的每批上限相同；即使重试复用检查点也重新核对，不静默丢尾。
    for batch_id in {entry["batch_id"] for entry in payload_entries}:
        group = [entry for entry in payload_entries if entry["batch_id"] == batch_id]
        if len(json.dumps(group, ensure_ascii=False, separators=(",", ":"))) > SUMMARY_REDUCE_BATCH_MAX_CHARS:
            raise AnalysisOutputError("text_too_long", "完整中间条目及原文证据超过预定汇总预算")
    return SummaryReduceRequest(
        system_prompt=summary_reduce_system_prompt(),
        user_prompt=build_reduce_user_prompt(document_name=document_name, version_id=version_id,
                                            batch_total=batch_total, entries=payload_entries),
        entries=entries)


def validate_reduce_output(raw: str, *, entries: list[dict]) -> ValidatedReduce:
    """校验汇总输出；`refs` 只能是本次输入的中间条目 ID。"""
    return validate_reduce(raw, entries=entries)


def resolve_reduce_points(reduced: ValidatedReduce, *, entries: list[dict]) -> tuple[
        list[ValidatedSummaryPoint], list[ValidatedSummaryPoint], list[dict]]:
    """把汇总结果的 `item_id` 引用沿链回落到原文单元。

    返回 (要点, 例外, 去重后的原文引用列表)。引用去重按 (block_id, source_index, quote)，
    编号在最终结果中重新连续分配；每个事实的 refs 指向这些最终编号。
    这条链保证：**最终引用始终回到原文连续子串，中间摘要不被当作原文**。
    """
    by_id = {entry["item_id"]: entry for entry in entries}
    citation_index: dict[tuple, int] = {}
    citations: list[dict] = []

    def resolve(points: list[ValidatedSummaryPoint]) -> list[ValidatedSummaryPoint]:
        resolved_points: list[ValidatedSummaryPoint] = []
        for point in points:
            refs: list[int] = []
            for item_id in point.refs:
                entry = by_id.get(item_id)
                if entry is None:
                    # 校验阶段已拒绝未知 item_id；这里再兜底一次，绝不静默丢弃引用。
                    raise AnalysisOutputError("unknown_reference", "汇总引用了未知中间条目")
                if not entry["original_refs"]:
                    raise AnalysisOutputError("missing_quote", "汇总事实不能引用没有原文依据的概述")
                for original in entry["original_refs"]:
                    key = (original["block_id"], original["source_index"], original["quote"])
                    if key not in citation_index:
                        citation_index[key] = len(citations) + 1
                        citations.append({"reference_id": citation_index[key], **original})
                    ref = citation_index[key]
                    if ref not in refs:
                        refs.append(ref)
            resolved_points.append(ValidatedSummaryPoint(text=point.text, refs=sorted(refs)))
        return resolved_points

    return resolve(reduced.main_points), resolve(reduced.exceptions), citations


def resolve_direct_points(points: list[ValidatedSummaryPoint], *,
                          batch: ValidatedSummaryBatch) -> tuple[list[ValidatedSummaryPoint], list[dict]]:
    """单批摘要的确定性解析：批内编号直接映射到原文单元引用，不经过汇总模型。

    单批摘要不需要汇总调用，因此最终引用与批内引述是同一层关系；
    这里重建连续编号并去重，保证与多批路径产出同构的结果结构。
    返回的引用只含编号、批内 ref 与引述；**来源定位由调用方通过服务端映射补齐**，
    本函数不伪造块 ID、页码或坐标。
    """
    citation_index: dict[int, int] = {}
    citations: list[dict] = []
    used: set[int] = set()
    for point in points:
        used.update(point.refs)
    for ref in sorted(used):
        quote = batch.quotes.get(ref)
        if quote is None:
            continue
        citation_index[ref] = len(citations) + 1
        citations.append({"reference_id": citation_index[ref], "ref": ref, "quote": quote})
    resolved = [ValidatedSummaryPoint(
        text=point.text,
        refs=sorted(citation_index[ref] for ref in point.refs if ref in citation_index))
        for point in points]
    return resolved, citations


__all__ = [
    "PROMPT_VERSION",
    "PROTOCOL_VERSION",
    "QUOTE_PREVIEW_CHARS",
    "SummaryBatchRequest",
    "SummaryReduceRequest",
    "build_batch_request",
    "build_reduce_request",
    "batch_entries",
    "resolve_direct_points",
    "resolve_reduce_points",
    "validate_batch",
    "validate_reduce_output",
]
