"""分析输入规划：由**解析版本**构建可分析单元、分批并给出请求数上界。

为什么不能复用 RAG 的检索路径（任务书 5.4、§7）：

- 摘要与提取要求“基于该解析版本完整合格输入生成”，检索 top-k 是相关性排序，
  用它冒充全文输入会把“只看了几段”包装成“全文摘要”。
- 因此这里遍历该解析版本的全部结构块，按阅读顺序构建连续输入单元，并显式给出
  覆盖口径（总输入单元、计划处理、排除的非正文单元及原因）。

设计要点：

1. **单元 = 结构块**。块的正文、表格渲染文本与来源一起固定；引用编号由服务端在
   每个批次内重新分配，编号只在本批有意义，跨批不共享（防止模型误映射）。
2. **不截断任何单元**。单元放不进单批预算时，本次规划直接给出受限原因，而不是
   从尾部截掉表头、单位、脚注或例外条件。
3. **调用前可验证的规划**。每个批次的字符数与消息长度用真实序列化结果计算；
   请求数上界 = 批次数 + （需要时）1 次汇总，必须在任务预算内。
4. **中间摘要不是原文**。汇总阶段只接收各批次**已校验条目**及其引述，最终引用
   仍回落到原文单元编号；这里只负责把映射关系固定下来。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from app.config import Settings
from app.analysis_prompts import (
    build_batch_user_prompt, build_reduce_user_prompt, summary_reduce_system_prompt,
    SUMMARY_REDUCE_BATCH_MAX_CHARS, SUMMARY_REDUCE_PROMPT_VERSION,
)
from app.rag_context import normalize_text
from app.schemas import Block, QualityWarning, SourceLocation

# 输入协议版本：单元编号语义、序列化字段含义发生变化时必须递增，
# 历史结果据此判断“当时按什么口径生成”。
INPUT_PROTOCOL_VERSION = "docqa-analysis-input-v1"

# 不进入可分析单元的块类型：页眉页脚属于版面噪声，图片未做语义解析，
# 空白块没有可核对内容。它们仍保留在预览中，并单独计入“排除的非正文单元”。
EXCLUDED_BLOCK_TYPES = {"page_header", "page_footer", "picture", "empty", "unknown"}
# 排除原因码 -> 中文说明（写入覆盖口径，页面直接展示）。
EXCLUDE_REASONS = {
    "page_header": "页眉属于版面信息，不作为事实依据",
    "page_footer": "页脚属于版面信息，不作为事实依据",
    "picture": "图片未做语义解析，图内文字与数据不作为可靠事实",
    "empty": "空块没有可核对内容",
    "unknown": "无法确定类型的块不作为事实依据",
}

# 公式占位块：解析器未给出公式内容时不补值，单元文本明确标注“缺失”。
FORMULA_PLACEHOLDER_TEXT = "（公式内容缺失，无法作为可靠事实）"

# 批次内单元序列化时的字段：ref 由服务端分配，content 是唯一可作为原文引用的字段。
UNIT_KIND_BLOCK_TYPE = "block_type"
UNIT_KIND_TABLE = "table"
UNIT_KIND_FORMULA = "formula_placeholder"

# 需要送入模型的现象独立成一批次的最小单位字符数下限：
# 单批预算必须至少容纳“最长单元 + 包装开销”，否则规划不可执行。
_MIN_BATCH_SLACK_CHARS = 400


@dataclass
class InputUnit:
    """一个不可分割的可分析输入单元：绑定唯一块与完整来源。"""
    unit_id: str
    batch_local_id: int
    block_id: str
    order_index: int
    kind: str
    block_type: str
    text: str
    heading_path: str | None
    sources: list[SourceLocation] = field(default_factory=list)
    # 每个来源在 sources 中的下标：引用卡片按该下标定位，避免模型提供坐标。
    source_ids: list[int] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        """序列化为送入模型的数据对象；只含可引用字段与服务端分配的编号。

        不包含块 ID、文档 ID、页码、坐标或文件路径：模型看不到内部标识，
        也就不可能伪造它们；引用卡片的定位信息全部由后端从本映射复制。
        """
        entry: dict[str, Any] = {"ref": self.batch_local_id, "content": self.text}
        if self.heading_path:
            entry["heading"] = self.heading_path
        entry["unit_kind"] = self.kind
        return entry


@dataclass
class UnitPlan:
    """一次规划的完整结果：单元、批次、排除项与受限原因。"""
    units: list[InputUnit] = field(default_factory=list)
    batches: list[list[InputUnit]] = field(default_factory=list)
    batch_message_chars: list[int] = field(default_factory=list)
    excluded_counts: dict[str, int] = field(default_factory=dict)
    # 无法放入任何批次的单元：给出稳定原因码，绝不为装入预算而截断。
    unplannable: list[tuple[InputUnit, str]] = field(default_factory=list)
    total_chars: int = 0
    excluded_units: int = 0


def _first_page(sources: list[SourceLocation]) -> int | None:
    for source in sources:
        if source.page:
            return source.page
    return None


def render_table_text(block: Block) -> str:
    """把表格块渲染为可引用文本，并保留表头、口径与缺缓存说明。

    表格是提取数据的主要来源，因此必须像分块阶段一样携带标题、脚注、工作表名、
    单元格范围与公式缓存状态；公式没有保存缓存时明确写“缺失（不能当作 0）”，
    绝不补 0，也不让模型据表达式推算新值。
    """
    table = block.table or {}
    parts: list[str] = []
    captions = [str(item).strip() for item in (table.get("captions") or []) if str(item).strip()]
    if captions:
        parts.append("表格标题：" + " / ".join(captions))
    if table.get("footnotes"):
        parts.append("注释：" + " / ".join(str(note) for note in table["footnotes"]))
    if block.sheet_name:
        parts.append(f"工作表：{block.sheet_name}")
    if table.get("table_no"):
        parts.append(f"表编号：{table['table_no']}")
    if table.get("cell_range"):
        parts.append(f"单元格范围：{table['cell_range']}")
    formulas = table.get("formulas") or {}
    if formulas:
        entries = []
        for coordinate, info in sorted(formulas.items()):
            cache = info.get("cached_value")
            cached = str(cache) if info.get("has_cache") else "缺失（不能当作 0）"
            entries.append(f"{coordinate}={info.get('formula')}（缓存：{cached}）")
        parts.append("公式：" + "；".join(entries))
    rows = _table_rows(block)
    if rows:
        parts.append("表格内容：\n" + rows)
    elif (block.text or "").strip():
        parts.append((block.text or "").strip())
    return "\n".join(part for part in parts if part)


def _table_rows(block: Block) -> str:
    """按行渲染表格；区分真实空白（空）与合并单元格占位（↳）。"""
    table = block.table or {}
    cells = table.get("cells") or []
    if not cells:
        return "\n".join(line for line in (block.text or "").splitlines() if line.strip())
    by_row: dict[int, list[tuple[int, str]]] = {}
    for cell in cells:
        row = cell.get("row")
        if not isinstance(row, int):
            continue
        col = cell.get("col") if isinstance(cell.get("col"), int) else 0
        by_row.setdefault(row, []).append((col, (cell.get("text") or "").strip()))
    lines: list[str] = []
    for row_index in sorted(by_row):
        values: list[str] = []
        for _col, text in sorted(by_row[row_index]):
            # 相邻数值相同不代表合并单元格；必须保留每一列的独立事实。
            values.append(text if text != "" else "（空）")
        lines.append(" | ".join(values))
    return "\n".join(lines)


def build_units(blocks: list[Block]) -> tuple[list[InputUnit], dict[str, int]]:
    """把解析版本的块转换为可分析单元，返回单元与排除计数。

    排除只针对明确不作为事实依据的块类型；排除原因逐类计数，页面据此说明覆盖。
    """
    units: list[InputUnit] = []
    excluded: dict[str, int] = {}
    for block in blocks:
        block_type = block.block_type or "unknown"
        text = ""
        kind = UNIT_KIND_BLOCK_TYPE
        if block_type == "table" or block_type == "document_index":
            text = render_table_text(block)
            kind = UNIT_KIND_TABLE
        elif block_type == "formula":
            # 公式内容缺失：保留占位单元并标注缺失，模型不得据此推算或补值。
            text = (block.text or "").strip() or FORMULA_PLACEHOLDER_TEXT
            kind = UNIT_KIND_FORMULA
        else:
            text = (block.text or "").strip()
        if block_type in EXCLUDED_BLOCK_TYPES or not text:
            reason = block_type if block_type in EXCLUDED_BLOCK_TYPES else "empty"
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        units.append(InputUnit(
            unit_id=block.id,
            batch_local_id=0,
            block_id=block.id,
            order_index=block.order_index,
            kind=kind,
            block_type=block_type,
            text=text,
            heading_path=block.heading_path,
            sources=list(block.sources),
            source_ids=list(range(len(block.sources))),
        ))
    return units, excluded


def _serialized_entry_chars(unit: InputUnit, *, quote_budget: int = 0) -> int:
    """单元连同可引用字段的序列化字符数（含 JSON 转义）。

    quote_budget 为最终会写回该编号的引述预留的字符数。分批阶段不需要预留，
    汇总阶段必须预留，否则“已校验检查点”可能在汇总时因包装超预算而无法装入。
    """
    entry = unit.payload()
    if quote_budget:
        entry["quote"] = "x" * quote_budget
    return len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


def plan_batches(units: list[InputUnit], *, batch_max_chars: int) -> tuple[list[list[InputUnit]], list[tuple[InputUnit, str]]]:
    """确定性顺序装入：按阅读顺序打包，单元不跨批、不截断。

    返回 (批次列表, 无法装入的单元及原因)。装不下时**跳过该单元并继续**，
    这样后续单元仍能被处理；无法装入的单元会进入未处理清单并如实计入覆盖。
    """
    batches: list[list[InputUnit]] = []
    unplannable: list[tuple[InputUnit, str]] = []
    current: list[InputUnit] = []
    current_chars = 2  # "[]" 两个字符
    for unit in units:
        size = _serialized_entry_chars(unit)
        if size + 2 > batch_max_chars:
            # 单元本身超过整批预算：不允许从尾部截断，标记为无法规划。
            unplannable.append((unit, "unit_over_batch_budget"))
            continue
        addition = size + (1 if current else 0)
        if current and current_chars + addition > batch_max_chars:
            batches.append(current)
            current = []
            current_chars = 2
            addition = size
        current.append(unit)
        current_chars += addition
    if current:
        batches.append(current)
    return batches, unplannable


@dataclass
class AnalysisPlanResult:
    """规划结果：与 AnalysisPlan 契约一一对应，但保留单元对象供执行阶段使用。"""
    kind: str
    prompt_version: str
    protocol_version: str
    fingerprint: str
    units: list[InputUnit]
    batches: list[list[InputUnit]]
    batch_message_chars: list[int]
    excluded_counts: dict[str, int]
    excluded_units: int
    total_chars: int
    unplannable: list[tuple[InputUnit, str]]
    reduce_required: bool
    request_upper_bound: int
    limitations: list[str] = field(default_factory=list)
    blocked_reason: str | None = None

    @property
    def executable(self) -> bool:
        return self.blocked_reason is None

    def planned_units(self) -> list[InputUnit]:
        """实际会被送入模型的单元（批次内单元的并集）。"""
        return [unit for batch in self.batches for unit in batch]


def _documents_payload(*, kind: str, document_name: str, version_id: str,
                       units: list[InputUnit]) -> dict[str, Any]:
    """批次 user 消息的数据部分：单元编号只在本批内有效。"""
    return {
        "task": "extract" if kind == "extraction" else "summary",
        "document_name": document_name,
        "parse_version": version_id,
        "units": [unit.payload() for unit in units],
    }


def batch_message_chars(*, kind: str, document_name: str, version_id: str,
                        units: list[InputUnit], system_prompt_chars: int,
                        batch_index: int = 1, batch_total: int = 1) -> int:
    """单批实际消息字符数：system + user（含真实 JSON 序列化与转义）。

    预算是**实际消息长度**，不是估算值；规划阶段与执行阶段必须使用同一口径，
    否则“规划说能装下、执行时超限”会让费用上界失去意义。
    """
    # 复用生产请求构造器，并使用真实的批内编号；元数据与转义也计入预算。
    payload_units = [{**unit.payload(), "ref": index + 1} for index, unit in enumerate(units)]
    user_chars = len(build_batch_user_prompt(
        kind=kind, document_name=document_name, version_id=version_id, units=payload_units,
        batch_index=batch_index, batch_total=batch_total))
    return system_prompt_chars + user_chars


def build_plan(*, kind: str, blocks: list[Block], document_name: str, version_id: str,
               settings: Settings, prompt_version: str, system_prompt_chars: int,
               parse_quality_status: str | None = None,
               parse_is_legacy: bool = False) -> AnalysisPlanResult:
    """构建完整输入规划，并判定是否可执行。

    不可执行的原因保持稳定且可由用户消除：
    - document_too_large：解析版本文本总量超过 DOCQA_ANALYSIS_MAX_DOCUMENT_CHARS；
    - batch_budget_too_small：单批预算装不下最长单元（需调大批次预算或缩小块）；
    - no_analyzable_content：没有可分析单元；
    - request_budget_exceeded：分批 + 汇总请求数超过任务预算或配置上界。
    """
    units, excluded = build_units(blocks)
    total_chars = sum(len(unit.text) for unit in units)
    excluded_units = sum(excluded.values())
    limitations: list[str] = []

    if parse_quality_status == "invalid":
        return AnalysisPlanResult(
            kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
            fingerprint="", units=[], batches=[], batch_message_chars=[], excluded_counts=excluded,
            excluded_units=excluded_units, total_chars=total_chars, unplannable=[],
            reduce_required=False, request_upper_bound=0,
            blocked_reason="parse_version_invalid")

    if parse_is_legacy:
        # legacy 版本由旧库迁移生成，只有旧的逻辑页分块，没有结构化块与来源。
        # 这里不补造来源，直接如实拒绝。
        return AnalysisPlanResult(
            kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
            fingerprint="", units=[], batches=[], batch_message_chars=[], excluded_counts=excluded,
            excluded_units=excluded_units, total_chars=total_chars, unplannable=[],
            reduce_required=False, request_upper_bound=0,
            blocked_reason="legacy_version_unsupported")

    if not units:
        return AnalysisPlanResult(
            kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
            fingerprint="", units=[], batches=[], batch_message_chars=[], excluded_counts=excluded,
            excluded_units=excluded_units, total_chars=total_chars, unplannable=[],
            reduce_required=False, request_upper_bound=0,
            blocked_reason="no_analyzable_content")

    if total_chars > settings.analysis_max_document_chars:
        return AnalysisPlanResult(
            kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
            fingerprint="", units=units, batches=[], batch_message_chars=[], excluded_counts=excluded,
            excluded_units=excluded_units, total_chars=total_chars, unplannable=[],
            reduce_required=False, request_upper_bound=0,
            blocked_reason="document_too_large")

    batches, unplannable = plan_batches(units, batch_max_chars=settings.analysis_batch_max_chars)
    message_chars = [batch_message_chars(kind=kind, document_name=document_name,
                                         version_id=version_id, units=batch,
                                         system_prompt_chars=system_prompt_chars,
                                         batch_index=index + 1, batch_total=len(batches))
                     for index, batch in enumerate(batches)]
    # 消息长度同样必须落在总输入预算内；超出说明批次预算或固定包装过大。
    over_budget = [(index, chars) for index, chars in enumerate(message_chars)
                   if chars > settings.analysis_input_max_chars]
    if over_budget:
        return AnalysisPlanResult(
            kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
            fingerprint="", units=units, batches=batches, batch_message_chars=message_chars,
            excluded_counts=excluded, excluded_units=excluded_units, total_chars=total_chars,
            unplannable=unplannable, reduce_required=False, request_upper_bound=0,
            blocked_reason="batch_message_over_input_budget")

    if unplannable:
        # 存在装不进任何批次的单元：本次不发布“全文”结果，明确列出未处理原因。
        limitations.append(
            f"有 {len(unplannable)} 个输入单元超过单批字符预算，无法在不截断表头、单位或条件的前提下送入模型；"
            "本次规划不完整，请调大 DOCQA_ANALYSIS_BATCH_MAX_CHARS 或缩小分块后重试")
        return AnalysisPlanResult(
            kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
            fingerprint="", units=units, batches=batches, batch_message_chars=message_chars,
            excluded_counts=excluded, excluded_units=excluded_units, total_chars=total_chars,
            unplannable=unplannable, reduce_required=False, request_upper_bound=0,
            limitations=limitations, blocked_reason="unit_over_batch_budget")

    # 多批摘要需要一次汇总调用；单批摘要直接出结果；提取无论多少批都用确定性合并。
    reduce_required = kind == "summary" and len(batches) > 1
    request_upper_bound = len(batches) + (1 if reduce_required else 0)
    if request_upper_bound > settings.analysis_max_requests:
        limitations.append(
            f"本次规划需要 {request_upper_bound} 次生成调用，超过单个分析任务上限 {settings.analysis_max_requests} 次；"
            "第一版不提供“只处理前几批却称为全文”的降级，也不自动拆成多个收费任务")
        return AnalysisPlanResult(
            kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
            fingerprint="", units=units, batches=batches, batch_message_chars=message_chars,
            excluded_counts=excluded, excluded_units=excluded_units, total_chars=total_chars,
            unplannable=unplannable, reduce_required=reduce_required,
            request_upper_bound=request_upper_bound, limitations=limitations,
            blocked_reason="request_budget_exceeded")

    if reduce_required:
        # 每批序列化上限包含完整引用和 JSON 转义；执行时同一上限再次校验。
        wrapper = build_reduce_user_prompt(document_name=document_name, version_id=version_id,
                                           batch_total=len(batches), entries=[])
        reduce_bound = (len(summary_reduce_system_prompt()) + len(wrapper)
                        + len(batches) * SUMMARY_REDUCE_BATCH_MAX_CHARS)
        if reduce_bound > min(settings.analysis_reduce_max_chars, settings.analysis_input_max_chars):
            return AnalysisPlanResult(
                kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
                fingerprint="", units=units, batches=batches, batch_message_chars=message_chars,
                excluded_counts=excluded, excluded_units=excluded_units, total_chars=total_chars,
                unplannable=unplannable, reduce_required=True, request_upper_bound=request_upper_bound,
                limitations=[f"汇总完整条目及证据的保守输入上界为 {reduce_bound} 字符，超过配置预算；"
                             "本次不发起任何调用，不截断证据或省略批次"],
                blocked_reason="reduce_message_over_input_budget")
        limitations.append(
            "长文档按批次生成已校验中间结果后再汇总；最终事实仍校验到原文连续子串，中间摘要不作为原文依据")
    if excluded_units:
        limitations.append(
            f"有 {excluded_units} 个非正文单元（页眉、页脚、图片等）未参与分析："
            "它们不作为事实依据，相关限制仍通过质量告警展示")
    if len(batches) == 1:
        limitations.append("本次输入可在一次调用内完成，未产生分批")

    fingerprint = plan_fingerprint(kind=kind, version_id=version_id, units=units,
                                   batches=batches, settings=settings,
                                   prompt_version=prompt_version)
    return AnalysisPlanResult(
        kind=kind, prompt_version=prompt_version, protocol_version=INPUT_PROTOCOL_VERSION,
        fingerprint=fingerprint, units=units, batches=batches, batch_message_chars=message_chars,
        excluded_counts=excluded, excluded_units=excluded_units, total_chars=total_chars,
        unplannable=unplannable, reduce_required=reduce_required,
        request_upper_bound=request_upper_bound, limitations=limitations, blocked_reason=None)


def plan_fingerprint(*, kind: str, version_id: str, units: list[InputUnit],
                     batches: list[list[InputUnit]], settings: Settings,
                     prompt_version: str) -> str:
    """计划指纹：版本、单元内容与顺序、分批结果和相关预算的稳定摘要。

    创建任务时固定它；若期间解析版本切换或预算配置变化导致指纹不一致，
    接口返回 409 并要求重新展示计划，避免静默扩大费用。
    """
    digest = sha256()
    digest.update(f"kind={kind};protocol={INPUT_PROTOCOL_VERSION};prompt={prompt_version};"
                  f"version={version_id};batch={settings.analysis_batch_max_chars};"
                  f"reduce={settings.analysis_reduce_max_chars};"
                  f"reduce_prompt={SUMMARY_REDUCE_PROMPT_VERSION};"
                  f"reduce_entry_limit={SUMMARY_REDUCE_BATCH_MAX_CHARS};"
                  f"input_limit={settings.analysis_input_max_chars};"
                  f"items={settings.analysis_max_items_per_batch},{settings.analysis_max_items_total};"
                  f"maxreq={settings.analysis_max_requests}".encode())
    for batch in batches:
        digest.update(b"|batch")
        for unit in batch:
            digest.update(unit.unit_id.encode())
            digest.update(unit.text.encode())
    digest.update(f"units={len(units)}".encode())
    return digest.hexdigest()


def reduce_batches(batches: list[list[InputUnit]], *, reduce_max_chars: int) -> list[list[list[InputUnit]]]:
    """把批次进一步分组，保证每组中间结果都能装入汇总预算。

    第一版对超长文档采用**单层多组汇总 + 最终合并**：每组先汇总一次，再把各组结果
    做一次确定性合并。这里返回的就是分组结果；调用方按 组数 + 1 计费。
    """
    groups: list[list[list[InputUnit]]] = []
    current: list[list[InputUnit]] = []
    current_chars = 2
    for batch in batches:
        size = sum(_serialized_entry_chars(unit) for unit in batch) + 2 * len(batch) + 2
        if current and current_chars + size > reduce_max_chars:
            groups.append(current)
            current = []
            current_chars = 2
        current.append(batch)
        current_chars += size
    if current:
        groups.append(current)
    return groups


def relevant_warnings(warnings: list[QualityWarning], units: list[InputUnit]) -> list[QualityWarning]:
    """保留与本次输入单元相关联的质量告警（公式缓存、OCR、表格限制等）。

    与 RAG 的同类逻辑不同：这里按**全部参与分析的单元**判定，而不是按检索命中，
    因为分析任务读取的是该解析版本的完整合格输入。
    """
    if not warnings:
        return []
    block_ids = {unit.block_id for unit in units}
    pages = {page for unit in units for page in
             [s.page for s in unit.sources] if page is not None}
    sheets = {s.sheet_name for unit in units for s in unit.sources if s.sheet_name}
    nodes = {s.node_ref for unit in units for s in unit.sources if s.node_ref}
    keep: list[QualityWarning] = []
    for warning in warnings:
        if warning.scope == "document":
            keep.append(warning)
            continue
        if warning.block_id and warning.block_id in block_ids:
            keep.append(warning)
            continue
        if warning.page is not None and warning.page in pages:
            keep.append(warning)
            continue
        if (warning.source and warning.source.node_ref in nodes) or \
                (warning.sheet_name and warning.sheet_name in sheets):
            keep.append(warning)
    return keep


__all__ = [
    "EXCLUDE_REASONS",
    "EXCLUDED_BLOCK_TYPES",
    "FORMULA_PLACEHOLDER_TEXT",
    "INPUT_PROTOCOL_VERSION",
    "AnalysisPlanResult",
    "InputUnit",
    "UnitPlan",
    "batch_message_chars",
    "build_plan",
    "build_units",
    "plan_fingerprint",
    "reduce_batches",
    "relevant_warnings",
    "render_table_text",
]
