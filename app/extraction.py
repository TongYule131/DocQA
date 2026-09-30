"""信息提取：数据 / 结论 / 观点的分批生成、校验与确定性合并。

本模块是**生产代码路径**，worker、脚本与测试都调用同一份实现，避免出现
“测试里另写一套提取逻辑”的假通过。

职责边界：

- 只负责“一批怎么问、怎么校验、多批怎么合并”，不碰数据库与任务状态；
- 合并是确定性的，按来源身份去重，不把另一批次的相同数字当成同一来源；
- 数值、单位、时间、主体与条件字段没有原文依据时保持 None，
  明确区分“原文未提及”与“明确不适用”，绝不默认 0。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.analysis_prompts import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_PROTOCOL_VERSION,
    build_batch_user_prompt,
    extraction_system_prompt,
)
from app.analysis_sources import InputUnit
from app.analysis_validation import (
    AnalysisOutputError,
    ValidatedExtraction,
    ValidatedItem,
    merge_extraction_items,
    validate_extraction_batch,
)

PROMPT_VERSION = EXTRACTION_PROMPT_VERSION
PROTOCOL_VERSION = EXTRACTION_PROTOCOL_VERSION
SECTION_LABELS = {"data": "数据", "conclusion": "结论", "viewpoint": "观点"}


@dataclass
class ExtractionBatchRequest:
    """一批提取请求：system 与 user 消息 + 本批服务端分配编号的单元。"""
    system_prompt: str
    user_prompt: str
    units: list[InputUnit]
    batch_index: int


def build_batch_request(*, units: list[InputUnit], document_name: str, version_id: str,
                        batch_index: int, batch_total: int) -> ExtractionBatchRequest:
    """构造一批提取请求。

    编号在**本批内**从 1 重新分配：单元对象在规划阶段已经写好 batch_local_id，
    这里只做防御性核对，确保送入模型的编号与本批单元一致。
    """
    for position, unit in enumerate(units, start=1):
        if unit.batch_local_id != position:
            raise ValueError("批次内单元编号必须从 1 连续分配")
    return ExtractionBatchRequest(
        system_prompt=extraction_system_prompt(),
        user_prompt=build_batch_user_prompt(
            kind="extraction", document_name=document_name, version_id=version_id,
            units=[unit.payload() for unit in units], batch_index=batch_index,
            batch_total=batch_total),
        units=units, batch_index=batch_index)


def validate_batch(raw: str, *, units: list[InputUnit], batch_id: str,
                   max_items: int) -> ValidatedExtraction:
    """校验一批提取输出；任何偏差都抛出 AnalysisOutputError，不做修补。"""
    return validate_extraction_batch(raw, units=units, batch_id=batch_id, max_items=max_items)


def merge_batches(batches: list[ValidatedExtraction], *,
                  unit_lookup) -> tuple[list[ValidatedItem], list[dict]]:
    """确定性地合并多批提取结果。

    `unit_lookup(batch_index, ref)` 返回该批次该编号对应的原文单元引用信息
    （块 ID、来源下标、引述与偏移）。来源编号在这里被**显式映射**，
    绝不允许把另一批次的相同数字当成同一来源。
    """
    return merge_extraction_items(batches, resolve_quote=unit_lookup)


def section_statuses(items: list[ValidatedItem], batches: list[ValidatedExtraction]) -> dict[str, str]:
    """汇总三类的最终状态。

    优先级：有提取项 → present；所有批次都成功看过输入且都没有该类 → none；
    否则 unprocessed（不允许把“没看到”说成“文档里没有”）。
    """
    statuses: dict[str, str] = {}
    for kind in ("data", "conclusion", "viewpoint"):
        if any(item.kind == kind for item in items):
            statuses[kind] = "present"
            continue
        if batches and all(batch.sections.get(kind) == "none" for batch in batches):
            statuses[kind] = "none"
        else:
            statuses[kind] = "unprocessed"
    return statuses


def collect_limitations(batches: list[ValidatedExtraction]) -> list[str]:
    """汇总各批次的限制说明，按首次出现顺序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for batch in batches:
        for text in batch.limitations:
            if text not in seen:
                seen.add(text)
                result.append(text)
    return result


__all__ = [
    "PROMPT_VERSION",
    "PROTOCOL_VERSION",
    "SECTION_LABELS",
    "ExtractionBatchRequest",
    "AnalysisOutputError",
    "build_batch_request",
    "collect_limitations",
    "merge_batches",
    "section_statuses",
    "validate_batch",
]
