"""分析任务模型输出的严格解析、结构与引用校验，以及确定性合并。

**这些检查只证明结构与来源可追溯，不能证明结论在语义上正确。**
“原文写 21、输出写 22，但编号与引述都真实”仍可能通过形式校验；这类错误必须由
独立语义复核发现并计为失败。摘要另拒绝引用中不存在的小数或百分数，以拦截新增计算；
这只是必要条件，不能验证主体、条件、因果或数字是否挂接正确，也不调用模型自评。

本模块做的都是确定性检查：

1. 严格解析：只接受单个 JSON 对象，可用一对 ```json 代码围栏包裹；不做正则捞取、
   不用 eval、不修补字段、不自动删除非法条目。
2. 结构校验：字段齐全、类型正确、拒绝未知字段、拒绝重复 JSON 键、
   拒绝布尔／浮点／字符串形式的引用编号。
3. 引用范围校验：编号必须属于**本次调用实际送入的单元**；跨批次编号一律拒绝，
   因为每个批次的编号是独立的，把它批的 3 号当成本批的 3 号是最危险的错引。
4. 原文引述校验：引述必须是所引用单元正文的连续子串（仅允许 CRLF→LF），
   不得删空格、改大小写、改数字、删标点来制造匹配；空引述与纯空白引述拒绝。
5. 字段依据校验：数据项的数值文本、单位、时间等非空事实字段必须能在其引用单元的
   引述中找到，不能主句有引用而数值或时间字段另行编造。
6. 汇总阶段更严：`refs` 只能引用本次输入中存在的中间条目 `item_id`，
   不允许模型在汇总时引入新的原文编号或新的引述。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.analysis_prompts import (
    MAX_FIELD_CHARS,
    MAX_ITEM_CONTENT_CHARS,
    MAX_QUOTE_CHARS,
    MAX_SUMMARY_EXCEPTIONS,
    MAX_SUMMARY_POINTS,
    MAX_SUMMARY_TEXT_CHARS,
)
from app.rag_context import normalize_text

# 稳定原因码：进入日志与任务错误码，不含原文、完整输出与凭证。
REASON_CODE_NAMES = {
    "invalid_json",
    "duplicate_key",
    "not_object",
    "unknown_field",
    "missing_field",
    "bad_type",
    "bad_kind",
    "bad_status_value",
    "items_too_many",
    "empty_items",
    "text_too_long",
    "field_too_long",
    "quote_too_long",
    "self_written_marker",
    "empty_reference_list",
    "non_integer_reference",
    "unknown_reference",
    "duplicate_quote",
    "unused_quote",
    "missing_quote",
    "empty_quote",
    "quote_not_found",
    "field_not_in_quote",
    "number_not_in_quote",
    "section_mismatch",
    "points_too_many",
    "exceptions_too_many",
    "quote_not_allowed",
    "empty_text",
}


class AnalysisOutputError(Exception):
    """模型输出未通过校验：携带稳定原因码，不携带原文内容。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"reason={reason}")
        self.reason = reason if reason in REASON_CODE_NAMES else "invalid_json"
        self.detail = detail


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    """object_pairs_hook：重复键必须被拒绝，而不是静默后者覆盖前者。"""
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise AnalysisOutputError("duplicate_key", f"重复 JSON 键：{key}")
        seen.add(key)
        result[key] = value
    return result


def _reject_constant(value: str):
    raise AnalysisOutputError("invalid_json", "模型输出包含非法数值常量")


def _strip_code_fence(raw: str) -> str:
    """只兼容“整个输出被一对 json 代码围栏包裹”，不支持夹带前言或后记。"""
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    opening = lines[0].strip().lower()
    if opening not in {"```", "```json"}:
        raise AnalysisOutputError("invalid_json", "代码围栏语言标记不受支持")
    closing_index = None
    for index in range(len(lines) - 1, 0, -1):
        if lines[index].strip() == "```":
            closing_index = index
            break
    if closing_index is None or closing_index != len(lines) - 1:
        raise AnalysisOutputError("invalid_json", "代码围栏未完整闭合或围栏外还有其他内容")
    return "\n".join(lines[1:closing_index]).strip()


def parse_model_output(raw: str) -> dict:
    """严格解析模型正文为单个 JSON 对象；任何偏差都抛出 AnalysisOutputError。"""
    if not isinstance(raw, str) or not raw.strip():
        raise AnalysisOutputError("invalid_json", "模型正文为空")
    body = _strip_code_fence(raw)
    if not body:
        raise AnalysisOutputError("invalid_json", "模型正文为空")
    try:
        # parse_constant 拒绝 NaN / Infinity：它们不是合法 JSON 数值。
        parsed = json.loads(body, object_pairs_hook=_reject_duplicate_keys,
                            parse_constant=_reject_constant)
    except AnalysisOutputError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError):
        raise AnalysisOutputError("invalid_json", "模型正文不是合法 JSON 对象") from None
    if not isinstance(parsed, dict):
        raise AnalysisOutputError("not_object", "模型输出不是 JSON 对象")
    return parsed


def _require_exact_keys(payload: dict, required: set[str], *, allow_extra: set[str] | None = None) -> None:
    """字段必须齐全；未知字段一律拒绝，避免模型夹带额外协议。"""
    keys = set(payload.keys())
    extra = keys - required - (allow_extra or set())
    if extra:
        raise AnalysisOutputError("unknown_field", "模型输出包含未知字段")
    missing = required - keys
    if missing:
        raise AnalysisOutputError("missing_field", "模型输出缺少必需字段")


def _require_str(value: Any, reason: str = "bad_type") -> str:
    if not isinstance(value, str):
        raise AnalysisOutputError(reason, "字段类型必须是字符串")
    return value


def _require_list(value: Any, reason: str = "bad_type") -> list:
    if not isinstance(value, list):
        raise AnalysisOutputError(reason, "字段类型必须是数组")
    return value


def _parse_ref(value: Any) -> int:
    """引用编号必须是真正的正整数：字符串、浮点、布尔值一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisOutputError("non_integer_reference", "引用编号必须是整数")
    if value <= 0:
        raise AnalysisOutputError("unknown_reference", "引用编号必须是正整数")
    return value


def _check_text(text: str, *, limit: int, allow_brackets: bool) -> str:
    value = normalize_text(text)
    if not value:
        raise AnalysisOutputError("empty_text", "文本不能为空")
    if len(value) > limit:
        raise AnalysisOutputError("text_too_long", "文本超过单条长度上限")
    if not allow_brackets and ("[" in value or "]" in value):
        # 引用标记只能由后端按 refs 生成，模型不得自己书写。
        raise AnalysisOutputError("self_written_marker", "文本中不允许出现方括号引用标记")
    return value


# ----------------------------------------------------------------------
# 分批校验：提取
# ----------------------------------------------------------------------
EXTRACTION_REQUIRED_KEYS = {"items", "quotes", "sections", "limitations"}
EXTRACTION_ITEM_REQUIRED = {"kind", "content", "refs"}
EXTRACTION_ITEM_OPTIONAL = {"name", "value_text", "unit", "period", "subject", "scope"}
# 需要能在引述中找到依据的“事实性字段”：防止主句有引用、数值或时间字段另行编造。
FACT_FIELDS = ("name", "value_text", "unit", "period", "subject", "scope")
ALLOWED_KINDS = {"data", "conclusion", "viewpoint"}
ALLOWED_SECTION_STATUS = {"present", "none"}


@dataclass
class ValidatedItem:
    """一条已校验的提取项：可选字段保持 None 表示原文未给出。"""
    item_id: str
    kind: str
    content: str
    name: str | None = None
    value_text: str | None = None
    unit: str | None = None
    period: str | None = None
    subject: str | None = None
    scope: str | None = None
    refs: list[int] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id, "kind": self.kind, "content": self.content,
            "name": self.name, "value_text": self.value_text, "unit": self.unit,
            "period": self.period, "subject": self.subject, "scope": self.scope,
            "refs": list(self.refs),
        }


@dataclass
class ValidatedExtraction:
    """一批提取的已校验结果。"""
    items: list[ValidatedItem]
    quotes: dict[int, str]
    sections: dict[str, str]
    limitations: list[str]


def _parse_extraction_item(raw: Any, *, units_by_ref: dict[int, Any], batch_id: str,
                           index: int, quote_text: dict[int, str]) -> ValidatedItem:
    if not isinstance(raw, dict):
        raise AnalysisOutputError("bad_type", "提取条目必须是对象")
    _require_exact_keys(raw, EXTRACTION_ITEM_REQUIRED, allow_extra=EXTRACTION_ITEM_OPTIONAL)
    kind = _require_str(raw.get("kind"))
    if kind not in ALLOWED_KINDS:
        raise AnalysisOutputError("bad_kind", "kind 取值必须是 data、conclusion 或 viewpoint")
    content = _check_text(_require_str(raw.get("content")), limit=MAX_ITEM_CONTENT_CHARS,
                          allow_brackets=False)
    refs_raw = _require_list(raw.get("refs"))
    if not refs_raw:
        raise AnalysisOutputError("empty_reference_list", "每条提取项必须有引用编号")
    refs: list[int] = []
    for value in refs_raw:
        ref = _parse_ref(value)
        if ref in refs:
            raise AnalysisOutputError("duplicate_quote", "同一条目内重复引用同一编号")
        if ref not in units_by_ref:
            # 跨批次编号拒绝：批内编号独立分配，他批的 n 号不是本批的 n 号。
            raise AnalysisOutputError("unknown_reference", "引用了本次输入范围之外的编号")
        refs.append(ref)
    fields: dict[str, str | None] = {}
    for name in FACT_FIELDS:
        value = raw.get(name)
        if value is None:
            fields[name] = None
            continue
        text = normalize_text(_require_str(value))
        if not text:
            # 空字符串等同于“未提及”：不允许用空串伪装成“明确不适用”。
            raise AnalysisOutputError("empty_text", f"字段 {name} 不能是空字符串，请写 null")
        if len(text) > MAX_FIELD_CHARS:
            raise AnalysisOutputError("field_too_long", f"字段 {name} 超过长度上限")
        if "[" in text or "]" in text:
            raise AnalysisOutputError("self_written_marker", f"字段 {name} 中不允许出现引用标记")
        fields[name] = text
    # 字段依据校验：非空字段必须能在其引述中被找到，避免字段另行编造。
    # 判定口径是 **任一被引用单元**：一条事实可能同时引用正文与表格，数值在表格里、
    # 期间在正文里，这是合法的；要求“每个字段都出现在每一条引述中”会把正确输出误判为失败。
    # 比较前只做空白归一化（CRLF 与首尾空白），不允许改字、删字或换数字。
    from app.analysis_prompts import FACT_FIELD_VALUE_WHITESPACE_TOLERANT

    def supported(value: str, quotes: list[str]) -> bool:
        if FACT_FIELD_VALUE_WHITESPACE_TOLERANT:
            squashed = "".join(value.split())
            return any(squashed and squashed in "".join(quote.split()) for quote in quotes)
        return any(value in quote for quote in quotes)

    quote_list = [quote_text.get(ref, "") for ref in refs]
    for name, text in fields.items():
        if text and not supported(text, quote_list):
            raise AnalysisOutputError(
                "field_not_in_quote", f"字段 {name} 的内容不能由所引用的原文引述支撑")
    return ValidatedItem(item_id=f"{batch_id}-i{index}", kind=kind, content=content,
                         refs=refs, **fields)


def validate_extraction_batch(raw: str, *, units: list[Any], batch_id: str,
                              max_items: int) -> ValidatedExtraction:
    """校验一批提取输出。

    units 是本次实际送入模型的单元列表（要求带 batch_local_id 与 text 属性）。
    任何偏差都抛出 AnalysisOutputError，且**不做任何修补**后继续。
    """
    payload = parse_model_output(raw)
    _require_exact_keys(payload, EXTRACTION_REQUIRED_KEYS)

    units_by_ref = {unit.batch_local_id: unit for unit in units}
    quotes = _parse_quotes(payload.get("quotes"), units_by_ref=units_by_ref)
    items_raw = _require_list(payload.get("items"))
    if len(items_raw) > max_items:
        # 超出上限受控失败：不截掉尾部条目再宣称完整。
        raise AnalysisOutputError("items_too_many", "单批提取条目数超过上限")

    sections_raw = payload.get("sections")
    if not isinstance(sections_raw, dict):
        raise AnalysisOutputError("bad_type", "sections 必须是对象")
    _require_exact_keys(sections_raw, ALLOWED_KINDS)
    sections: dict[str, str] = {}
    for kind in ALLOWED_KINDS:
        value = _require_str(sections_raw.get(kind))
        if value not in ALLOWED_SECTION_STATUS:
            raise AnalysisOutputError("bad_status_value", "sections 取值只能是 present 或 none")
        sections[kind] = value

    limitations = _parse_limitations(payload.get("limitations"))

    items = [_parse_extraction_item(raw_item, units_by_ref=units_by_ref, batch_id=batch_id,
                                    index=position, quote_text=quotes)
             for position, raw_item in enumerate(items_raw, start=1)]
    # sections 必须与实际条目一致：有该类条目却写 none（或反之）会让“某类不存在”失去意义。
    for kind in ALLOWED_KINDS:
        has_kind = any(item.kind == kind for item in items)
        if has_kind and sections[kind] != "present":
            raise AnalysisOutputError("section_mismatch", "sections 与 items 不一致")
        if not has_kind and sections[kind] != "none":
            raise AnalysisOutputError("section_mismatch", "sections 与 items 不一致")

    validated = ValidatedExtraction(items=items, quotes=quotes, sections=sections,
                                   limitations=limitations)
    _check_quote_usage(validated, units_by_ref=units_by_ref)
    return validated


# ----------------------------------------------------------------------
# 分批校验：摘要
# ----------------------------------------------------------------------
SUMMARY_BATCH_REQUIRED_KEYS = {"topic_overview", "main_points", "exceptions", "quotes", "limitations"}


@dataclass
class ValidatedSummaryPoint:
    text: str
    refs: list[int]


@dataclass
class ValidatedSummaryBatch:
    topic_overview: str
    main_points: list[ValidatedSummaryPoint]
    exceptions: list[ValidatedSummaryPoint]
    quotes: dict[int, str]
    limitations: list[str]


def _parse_points(raw_value: Any, *, units_by_ref: dict[int, Any], section: str,
                  limit: int) -> list[ValidatedSummaryPoint]:
    points: list[ValidatedSummaryPoint] = []
    for raw in _require_list(raw_value):
        if not isinstance(raw, dict):
            raise AnalysisOutputError("bad_type", f"{section} 条目必须是对象")
        _require_exact_keys(raw, {"text", "refs"})
        text = _check_text(_require_str(raw.get("text")), limit=MAX_SUMMARY_TEXT_CHARS,
                           allow_brackets=False)
        refs_raw = _require_list(raw.get("refs"))
        if not refs_raw:
            raise AnalysisOutputError("empty_reference_list", f"{section} 的每条内容必须有引用编号")
        refs: list[int] = []
        for value in refs_raw:
            ref = _parse_ref(value)
            if ref in refs:
                raise AnalysisOutputError("duplicate_quote", f"{section} 内重复引用同一编号")
            if ref not in units_by_ref:
                raise AnalysisOutputError("unknown_reference", "引用了本次输入范围之外的编号")
            refs.append(ref)
        points.append(ValidatedSummaryPoint(text=text, refs=refs))
    if len(points) > limit:
        raise AnalysisOutputError("points_too_many", f"{section} 条目数超过上限")
    return points


def validate_summary_batch(raw: str, *, units: list[Any], batch_id: str) -> ValidatedSummaryBatch:
    """校验一批摘要输出；结构与引用规则与提取批保持一致。"""
    payload = parse_model_output(raw)
    _require_exact_keys(payload, SUMMARY_BATCH_REQUIRED_KEYS)
    units_by_ref = {unit.batch_local_id: unit for unit in units}
    quotes = _parse_quotes(payload.get("quotes"), units_by_ref=units_by_ref)
    topic = normalize_text(_require_str(payload.get("topic_overview")))
    if not topic:
        raise AnalysisOutputError("empty_text", "topic_overview 不能为空")
    if len(topic) > MAX_SUMMARY_TEXT_CHARS * 2:
        raise AnalysisOutputError("text_too_long", "topic_overview 超过长度上限")
    main_points = _parse_points(payload.get("main_points"), units_by_ref=units_by_ref,
                                section="main_points", limit=12)
    exceptions = _parse_points(payload.get("exceptions"), units_by_ref=units_by_ref,
                               section="exceptions", limit=8)
    if not main_points:
        raise AnalysisOutputError("empty_items", "main_points 至少需要一条带引用的内容")
    limitations = _parse_limitations(payload.get("limitations"))
    validated = ValidatedSummaryBatch(topic_overview=topic, main_points=main_points,
                                      exceptions=exceptions, quotes=quotes,
                                      limitations=limitations)
    _check_quote_usage(validated, units_by_ref=units_by_ref)
    for point in main_points + exceptions:
        _check_summary_numbers(point.text, [quotes[ref] for ref in point.refs])
    return validated


# ----------------------------------------------------------------------
# 汇总校验（只允许引用已校验的中间条目 ID）
# ----------------------------------------------------------------------
REDUCE_REQUIRED_KEYS = {"topic_overview", "main_points", "exceptions", "limitations"}


@dataclass
class ValidatedReduce:
    topic_overview: str
    main_points: list[ValidatedSummaryPoint]
    exceptions: list[ValidatedSummaryPoint]
    limitations: list[str]


def validate_reduce(raw: str, *, entries: list[dict]) -> ValidatedReduce:
    """校验汇总输出。

    与分批校验的关键区别：
    - 不允许出现 `quotes`：中间摘要是模型派生内容，不是原文，不能为最终事实提供引述；
    - `refs` 元素是输入中间条目的 `item_id` 字符串，必须属于本次输入集合，
      这样最终引用一定能沿链回落到原文单元。
    """
    payload = parse_model_output(raw)
    _require_exact_keys(payload, REDUCE_REQUIRED_KEYS)
    known_ids = {entry.get("item_id") for entry in entries}
    topic = normalize_text(_require_str(payload.get("topic_overview")))
    if not topic:
        raise AnalysisOutputError("empty_text", "topic_overview 不能为空")
    if len(topic) > MAX_SUMMARY_TEXT_CHARS * 2:
        raise AnalysisOutputError("text_too_long", "topic_overview 超过长度上限")

    def parse_points(value: Any, section: str, limit: int) -> list[ValidatedSummaryPoint]:
        points: list[ValidatedSummaryPoint] = []
        for raw_item in _require_list(value):
            if not isinstance(raw_item, dict):
                raise AnalysisOutputError("bad_type", f"{section} 条目必须是对象")
            _require_exact_keys(raw_item, {"text", "refs"})
            text = _check_text(_require_str(raw_item.get("text")), limit=MAX_SUMMARY_TEXT_CHARS,
                               allow_brackets=False)
            refs_raw = _require_list(raw_item.get("refs"))
            if not refs_raw:
                raise AnalysisOutputError("empty_reference_list", "汇总条目必须声明来源条目")
            refs: list[str] = []
            for value_item in refs_raw:
                if not isinstance(value_item, str) or not value_item.strip():
                    raise AnalysisOutputError("bad_type", "汇总 refs 必须是条目 ID 字符串")
                item_id = value_item.strip()
                if item_id not in known_ids:
                    raise AnalysisOutputError("unknown_reference",
                                              "汇总引用了本次输入之外的中间条目")
                if item_id in refs:
                    raise AnalysisOutputError("duplicate_quote", "汇总条目内重复引用同一条目")
                refs.append(item_id)
            points.append(ValidatedSummaryPoint(text=text, refs=refs))
        if len(points) > limit:
            raise AnalysisOutputError("points_too_many", f"{section} 条目数超过上限")
        return points

    main_points = parse_points(payload.get("main_points"), "main_points", MAX_SUMMARY_POINTS)
    exceptions = parse_points(payload.get("exceptions"), "exceptions", MAX_SUMMARY_EXCEPTIONS)
    if not main_points:
        raise AnalysisOutputError("empty_items", "main_points 至少需要一条内容")
    by_id = {entry["item_id"]: entry for entry in entries}
    for point in main_points + exceptions:
        originals = [source["quote"] for ref in point.refs
                     for source in by_id[ref].get("original_refs", [])]
        if not originals:
            raise AnalysisOutputError("missing_quote", "汇总事实缺少原文证据")
        _check_summary_numbers(point.text, originals)
    return ValidatedReduce(topic_overview=topic, main_points=main_points, exceptions=exceptions,
                           limitations=_parse_limitations(payload.get("limitations")))


def _check_summary_numbers(text: str, quotes: list[str]) -> None:
    """拦截新增小数/百分数；不把这一必要条件冒充完整数字与语义校验。"""
    import re

    pattern = r"(?<![\d.])[-+−]?\d+(?:\.\d+)?(?:[%％])?(?![\d.])"
    original_numbers = {number for quote in quotes for number in re.findall(pattern, quote)}
    # “表格未列示 2024 年”是可核对的缺项说明，2024 不在该表引用里并不意味着编造。
    # 保守守卫只拒绝新增小数/百分数，整数、年份、主体与否定仍需独立语义复核。
    claimed_values = {number for number in re.findall(pattern, text)
                      if "." in number or "%" in number or "％" in number}
    if claimed_values - original_numbers:
        raise AnalysisOutputError("number_not_in_quote", "摘要数值未出现在所引用的原文中")


# ----------------------------------------------------------------------
# 共用辅助
# ----------------------------------------------------------------------
def _parse_quotes(raw_value: Any, *, units_by_ref: dict[int, Any]) -> dict[int, str]:
    """解析并校验引述：每条恰好对应一个编号，且必须是该单元正文的连续子串。

    只接受“编号字符串 -> 引述”的对象形式。这与 RAG 的数组协议不同是有意的：
    分析任务的引用编号在**批次内**分配，用对象形式能让“一个编号恰好一条引述”
    在 JSON 层面就无法表达重复键（重复键会在解析阶段被拒绝）。
    """
    quotes: dict[int, str] = {}
    if not isinstance(raw_value, dict):
        raise AnalysisOutputError("bad_type", "quotes 必须是“编号 -> 引述”的 JSON 对象")
    for key, value in raw_value.items():
        try:
            ref = int(key)
        except (TypeError, ValueError):
            raise AnalysisOutputError("non_integer_reference", "引述键必须是单元编号") from None
        if ref <= 0:
            raise AnalysisOutputError("unknown_reference", "引述编号必须是正整数")
        unit = units_by_ref.get(ref)
        if unit is None:
            # 跨批次编号或越界编号一律拒绝：他批的 n 号不是本批的 n 号。
            raise AnalysisOutputError("unknown_reference", "引述引用了本次输入范围之外的编号")
        if ref in quotes:
            raise AnalysisOutputError("duplicate_quote", "同一引用编号出现多条引述")
        quote = _require_str(value).replace("\r\n", "\n")
        if not quote.strip():
            raise AnalysisOutputError("empty_quote", "引述不能为空或纯空白")
        if len(quote) > MAX_QUOTE_CHARS:
            raise AnalysisOutputError("quote_too_long", "引述超过单条长度上限")
        # 只允许统一 CRLF：不删空格、不改大小写、不改数字、不删标点。
        if quote not in unit.text.replace("\r\n", "\n"):
            raise AnalysisOutputError("quote_not_found", "引述不是对应单元正文的连续子串")
        quotes[ref] = quote
    return quotes


def _check_quote_usage(validated, *, units_by_ref: dict[int, Any]) -> None:
    """引用使用规则：每个被使用的编号恰好对应一条引述。

    - 有提取项／要点时：必须有引用，且不得出现未被使用的引述；
    - 一批**完全没有**提取项时（已覆盖输入但没有该类内容）：允许 refs 与 quotes 都为空，
      这是合法的 `no_extractable_items`；但只要给出了引述，就必须被使用。
    """
    used: set[int] = set()
    for point in list(getattr(validated, "main_points", [])) + list(getattr(validated, "exceptions", [])):
        used.update(point.refs)
    for item in getattr(validated, "items", []):
        used.update(item.refs)
    if used:
        missing = used - set(validated.quotes.keys())
        if missing:
            raise AnalysisOutputError("missing_quote", "被使用的编号缺少对应引述")
    unused = set(validated.quotes.keys()) - used
    if unused:
        raise AnalysisOutputError("unused_quote", "存在未被任何内容使用的引述")


def _parse_limitations(raw_value: Any) -> list[str]:
    limitations: list[str] = []
    for raw in _require_list(raw_value):
        text = _check_text(_require_str(raw), limit=MAX_SUMMARY_TEXT_CHARS, allow_brackets=True)
        limitations.append(text)
    if len(limitations) > 12:
        raise AnalysisOutputError("text_too_long", "限制说明条数超过上限")
    return limitations


# ----------------------------------------------------------------------
# 确定性合并（提取）
# ----------------------------------------------------------------------
def merge_extraction_items(batches: list[ValidatedExtraction], *,
                           resolve_quote) -> tuple[list[ValidatedItem], list[dict]]:
    """确定性地合并多批提取结果，并显式映射来源编号。

    `resolve_quote(batch_index, ref)` 由调用方提供：它把“第几批的第几号”映射回
    原文单元的引述与来源。**绝不把另一个批次的相同数字当成同一来源。**

    去重键包含类别、内容、对象、时间与条件以及来源身份：数值相同但主体、时间或
    口径不同的两条事实不会被错误合并，也不会丢失任一来源。

    返回的条目 `refs` 指向**最终引用编号**（从 1 连续分配），引用清单也只包含
    被实际使用的来源；这样页面、导出与校验使用的是同一套编号，不会出现“模型编
    号”与“展示编号”两套体系。
    """
    merged: list[ValidatedItem] = []
    seen_keys: set[tuple] = set()
    citation_index: dict[tuple, int] = {}
    citations: list[dict] = []
    for batch_index, batch in enumerate(batches):
        for item in batch.items:
            resolved = [resolve_quote(batch_index, ref) for ref in item.refs]
            source_keys = tuple(sorted({
                (entry["block_id"], entry.get("source_index"), entry["quote"])
                for entry in resolved}))
            key = (item.kind, item.content, item.name, item.value_text, item.unit,
                   item.period, item.subject, item.scope, source_keys)
            if key in seen_keys:
                # 完全相同的条目（含来源身份）只保留一次；不同来源的同文条款保留两条。
                continue
            seen_keys.add(key)
            final_refs: list[int] = []
            for entry in resolved:
                source_key = (entry["block_id"], entry.get("source_index"), entry["quote"])
                if source_key not in citation_index:
                    citation_index[source_key] = len(citations) + 1
                    citations.append({
                        "reference_id": citation_index[source_key],
                        "block_id": entry["block_id"],
                        "source_index": entry.get("source_index"),
                        "block_type": entry.get("block_type"),
                        "quote": entry["quote"],
                        "sources": entry.get("sources", []),
                        "char_start": entry.get("char_start"),
                        "char_end": entry.get("char_end"),
                    })
                ref = citation_index[source_key]
                if ref not in final_refs:
                    final_refs.append(ref)
            merged.append(ValidatedItem(
                item_id=item.item_id, kind=item.kind, content=item.content,
                name=item.name, value_text=item.value_text, unit=item.unit,
                period=item.period, subject=item.subject, scope=item.scope,
                refs=sorted(final_refs)))
    return merged, citations


def merge_summary_points(batches: list[ValidatedSummaryBatch]) -> tuple[list[ValidatedSummaryPoint], list[ValidatedSummaryPoint]]:
    """单层确定性合并（用于汇总调用不可用时的受控路径）。

    只在**不丢失信息**的前提下按文本去重：来源编号直接合并，重复表述保留一次。
    """
    def merge(points: list[ValidatedSummaryPoint]) -> list[ValidatedSummaryPoint]:
        by_text: dict[str, ValidatedSummaryPoint] = {}
        order: list[str] = []
        for point in points:
            existing = by_text.get(point.text)
            if existing is None:
                by_text[point.text] = ValidatedSummaryPoint(text=point.text, refs=list(point.refs))
                order.append(point.text)
            else:
                for ref in point.refs:
                    if ref not in existing.refs:
                        existing.refs.append(ref)
        return [by_text[text] for text in order]

    main: list[ValidatedSummaryPoint] = []
    exceptions: list[ValidatedSummaryPoint] = []
    for batch in batches:
        main.extend(batch.main_points)
        exceptions.extend(batch.exceptions)
    return merge(main), merge(exceptions)


__all__ = [
    "AnalysisOutputError",
    "FACT_FIELDS",
    "REASON_CODE_NAMES",
    "ValidatedExtraction",
    "ValidatedItem",
    "ValidatedReduce",
    "ValidatedSummaryBatch",
    "ValidatedSummaryPoint",
    "merge_extraction_items",
    "merge_summary_points",
    "parse_model_output",
    "validate_extraction_batch",
    "validate_reduce",
    "validate_summary_batch",
]
