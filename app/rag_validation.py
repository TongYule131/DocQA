"""RAG 模型输出解析与后端引用校验（工程包 C/D）。

必须明确的边界（任务书第 7 节）：

**这些检查只证明结构与来源可追溯，不能证明引用在语义上支持结论。**
“原文为 21、答案写 22，但引用编号与引述都真实”仍可能通过形式校验；
这类错误必须由语义评估（`scripts/evaluate_rag.py`）发现并计为失败。
因此这里不使用字符串包含、数字集合检查或第二次大模型自评来冒充事实校验器，
也不在本阶段增加隐藏的第二次模型调用。

本模块只做确定性校验：
1. 严格解析：只接受单个 JSON 对象，可用一对 ```json 代码围栏包裹；不做正则捞取、
   不用 eval、不修补字段、不自动删除错误引用。
2. 结构校验：字段齐全、类型正确、拒绝未知字段、拒绝重复 JSON 键、拒绝布尔/浮点编号。
3. 引用范围校验：编号必须是本次**最终入选证据**中存在的编号；候选中存在但未送入
   模型的块一律不可引用。
4. 原文引述校验：quote 必须是对应证据正文的连续子串（仅统一 CRLF 为 LF），
   不得删空格、改大小写、改数字或删标点来制造匹配；空引述与纯空白引述拒绝。
5. 一致性校验：每个被使用的编号恰好对应一条引述；未使用的引述、缺失的引述、
   冲突的重复定义、模型自写引用标记一律拒绝。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.rag_context import EvidencePack, normalize_text

# 校验失败原因码：进入日志与错误码，不含原文、完整输出与凭证。
REASON_CODE_NAMES = {
    'composition_plan_invalid','composition_reference_invalid','composition_coverage_invalid',
    'composition_layout_mismatch','composition_comparison_unpaired','composition_source_missing',
    'composition_checkpoint_changed','composition_binding_invalid','composition_process_coverage_invalid',
    'presentation_missing','presentation_invalid','presentation_reference_invalid','presentation_coverage_mismatch',
    "unsupported_contract_version",
    "invalid_json",
    "duplicate_key",
    "not_object",
    "unknown_field",
    "missing_field",
    "bad_type",
    "bad_status",
    "status_conflict",
    "facts_too_many",
    "empty_conclusion",
    "empty_explanation",
    "question_count",
    "text_too_long",
    "quote_too_long",
    "self_written_marker",
    "empty_reference_list",
    "unknown_reference",
    "duplicate_quote",
    "unused_quote",
    "missing_quote",
    "empty_quote",
    "quote_not_found",
    "mixed_user_source",
    "coverage_missing", "fact_context_missing", "context_scope_mismatch", "context_not_in_quote",
    "numeric_context_missing", "number_not_in_quote", "coverage_mismatch",
    "number_unit_mismatch",
    "page_locator_not_supported",
    "statement_locator_not_supported",
    "locator_not_supported",
    "unsupported_requirement", "coverage_status_conflict",
    "claim_evidence_invalid", "claim_type_missing_or_invalid", "context_not_in_claim_evidence",
    'quote_not_in_admitted_input','incomplete_quote_sentence','continuation_not_bound_to_claim','inference_label_required',
}

# 数量与长度上限（对应任务书 6.2）。
MAX_FACT_TEXT_CHARS = 1000
MAX_QUESTION_CHARS = 500
MAX_TOTAL_TEXT_CHARS = 4000
MAX_QUOTE_CHARS = 300
MAX_FACT_ITEMS = 10

_REQUIRED_KEYS = {"status", "conclusion", "explanation", "clarification_questions", "evidence_quotes"}
_ALLOWED_STATUS = {"answered", "clarification_needed", "insufficient_evidence"}


class ModelOutputError(Exception):
    """模型输出未通过校验：携带稳定原因码，不携带原文内容。"""

    def __init__(self, reason: str, detail: str = "", *, location: dict | None = None):
        super().__init__(f"reason={reason}")
        self.reason = reason if reason in REASON_CODE_NAMES else "invalid_json"
        self.detail = detail
        self.location = location or {}


@dataclass
class ValidatedFact:
    """已校验的事实条目：text 与 refs 都通过结构与范围检查。"""
    text: str
    refs: list[int]
    context: list[dict] = field(default_factory=list)
    claim_type: str = "unclassified"
    support_review: dict = field(default_factory=dict)


@dataclass
class ValidatedOutput:
    """已校验的模型输出（内部结构，不能直接作为 HTTP 响应）。"""
    status: str
    conclusion: list[ValidatedFact] = field(default_factory=list)
    explanation: list[ValidatedFact] = field(default_factory=list)
    clarification_questions: list[str] = field(default_factory=list)
    quotes: dict[int, str] = field(default_factory=dict)
    missing_points: list[str] = field(default_factory=list)
    answer_parts: list[dict] = field(default_factory=list)
    context_selection_count: int = 0
    # v4的展示引用可以来自同一输入片段的不同范围；始终保留真实输入ref和偏移。
    citation_bindings: dict[int, dict] = field(default_factory=dict)
    presentation: dict | None = None

    def used_refs(self) -> set[int]:
        refs: set[int] = set()
        for fact in list(self.conclusion) + list(self.explanation):
            refs.update(fact.refs)
        return refs


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    """object_pairs_hook：重复键必须被拒绝，而不是静默后者覆盖前者。"""
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ModelOutputError("duplicate_key", f"重复 JSON 键：{key}")
        seen.add(key)
        result[key] = value
    return result


def _strip_code_fence(raw: str) -> str:
    """只兼容“整个输出被一对 json 代码围栏包裹”的格式。

    不支持“前言 + JSON + 后记”，也不从混合文本里用正则捞出 JSON。
    """
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    opening = lines[0].strip().lower()
    if opening not in {"```", "```json"}:
        raise ModelOutputError("invalid_json", "代码围栏语言标记不受支持")
    # 结尾必须是最后一行的独立围栏，否则视为夹带其他内容。
    closing_index = None
    for index in range(len(lines) - 1, 0, -1):
        if lines[index].strip() == "```":
            closing_index = index
            break
    if closing_index is None or closing_index != len(lines) - 1:
        raise ModelOutputError("invalid_json", "代码围栏未完整闭合或围栏外还有其他内容")
    return "\n".join(lines[1:closing_index]).strip()


def parse_model_output(raw: str) -> dict:
    """严格解析模型正文为单个 JSON 对象；任何偏差都抛出 ModelOutputError。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ModelOutputError("invalid_json", "模型正文为空")
    body = _strip_code_fence(raw)
    if not body:
        raise ModelOutputError("invalid_json", "模型正文为空")
    try:
        # parse_constant 拒绝 NaN / Infinity：它们不是合法 JSON 数值。
        parsed = json.loads(body, object_pairs_hook=_reject_duplicate_keys,
                            parse_constant=_reject_constant)
    except ModelOutputError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError):
        raise ModelOutputError("invalid_json", "模型正文不是合法 JSON 对象") from None
    if not isinstance(parsed, dict):
        raise ModelOutputError("not_object", "模型输出不是 JSON 对象")
    return parsed


def _reject_constant(value: str):
    raise ModelOutputError("invalid_json", "模型输出包含非法数值常量")


def _require_keys(payload: dict) -> None:
    """字段必须齐全；未知字段一律拒绝，避免模型夹带额外协议。"""
    keys = set(payload.keys())
    extra = keys - _REQUIRED_KEYS
    if extra:
        raise ModelOutputError("unknown_field", "模型输出包含未知字段")
    missing = _REQUIRED_KEYS - keys
    if missing:
        raise ModelOutputError("missing_field", "模型输出缺少必需字段")


def _require_list(value: Any, reason: str = "bad_type") -> list:
    if not isinstance(value, list):
        raise ModelOutputError(reason, "字段类型必须是数组")
    return value


def _require_str(value: Any, reason: str = "bad_type") -> str:
    if not isinstance(value, str):
        raise ModelOutputError(reason, "字段类型必须是字符串")
    return value


def _parse_ref(value: Any) -> int:
    """引用编号必须是真正的正整数：字符串、浮点、布尔值一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelOutputError("bad_type", "引用编号必须是整数")
    if value <= 0:
        raise ModelOutputError("unknown_reference", "引用编号必须是正整数")
    return value


def _parse_fact(raw: Any, *, evidence: EvidencePack | None,
                total_chars: list[int]) -> ValidatedFact:
    """解析并校验单条事实：字段严格、text 非空、refs 非空且必须存在。"""
    if not isinstance(raw, dict):
        raise ModelOutputError("bad_type", "事实条目必须是对象")
    keys = set(raw.keys())
    if keys - {"text", "refs"}:
        raise ModelOutputError("unknown_field", "事实条目包含未知字段")
    if keys != {"text", "refs"}:
        raise ModelOutputError("missing_field", "事实条目缺少 text 或 refs")
    text = normalize_text(_require_str(raw.get("text")))
    if not text:
        raise ModelOutputError("bad_type", "事实文本不能为空")
    if len(text) > MAX_FACT_TEXT_CHARS:
        raise ModelOutputError("text_too_long", "事实文本超过单条长度上限")
    # 模型不得自己写引用标记：编号只能由后端按 refs 渲染。
    if "[" in text or "]" in text:
        raise ModelOutputError("self_written_marker", "事实文本中不允许出现方括号引用标记")
    raw_refs = _require_list(raw.get("refs"))
    if not raw_refs:
        raise ModelOutputError("empty_reference_list", "每条事实必须有引用编号")
    refs: list[int] = []
    for item in raw_refs:
        ref = _parse_ref(item)
        if ref in refs:
            raise ModelOutputError("duplicate_quote", "同一事实内重复引用同一编号")
        if evidence is not None and evidence.item_by_ref(ref) is None:
            # 候选中存在但未送入模型的编号同样不可引用。
            raise ModelOutputError("unknown_reference", "引用了本次证据范围之外的编号")
        refs.append(ref)
    total_chars[0] += len(text)
    if total_chars[0] > MAX_TOTAL_TEXT_CHARS:
        raise ModelOutputError("text_too_long", "全部事实与问题文本合计超过上限")
    return ValidatedFact(text=text, refs=refs)


def _parse_questions(raw_value: Any, *, total_chars: list[int]) -> list[str]:
    """解析澄清问题：1～2 个时由状态规则进一步约束，文本不得夹带引用标记。"""
    questions: list[str] = []
    for raw in _require_list(raw_value):
        text = normalize_text(_require_str(raw))
        if not text:
            raise ModelOutputError("bad_type", "澄清问题不能为空")
        if len(text) > MAX_QUESTION_CHARS:
            raise ModelOutputError("text_too_long", "单个澄清问题超过长度上限")
        if "[" in text or "]" in text:
            raise ModelOutputError("self_written_marker", "澄清问题中不允许出现方括号引用标记")
        total_chars[0] += len(text)
        if total_chars[0] > MAX_TOTAL_TEXT_CHARS:
            raise ModelOutputError("text_too_long", "全部事实与问题文本合计超过上限")
        questions.append(text)
    return questions


def _parse_quotes(raw_value: Any, *, evidence: EvidencePack, quote_limit: int = MAX_QUOTE_CHARS,
                  allow_spans: bool = False) -> dict[int, str]:
    """解析并校验引述：每条恰好对应一个编号，且必须是证据正文的连续子串。"""
    quotes: dict[int, str] = {}
    for raw in _require_list(raw_value):
        if not isinstance(raw, dict):
            raise ModelOutputError("bad_type", "引述条目必须是对象")
        keys = set(raw.keys())
        if allow_spans and keys == {"ref", "span"}:
            from app.quote_spans import quote_choices
            ref = _parse_ref(raw["ref"])
            item = evidence.item_by_ref(ref)
            if item is None:
                raise ModelOutputError("unknown_reference", "引述引用了本次证据范围之外的编号")
            span = raw["span"]
            spans = quote_choices(item.text)
            if type(span) is not int or not 1 <= span <= len(spans):
                raise ModelOutputError("bad_type", "引述编号必须属于本次证据中真实存在的候选")
            # 回取原文而非修补模型字符串；长度、唯一编号和逐事实引用继续走原严格校验。
            raw = {"ref": ref, "quote": spans[span - 1]}
            keys = set(raw)
        if keys - {"ref", "quote"}:
            raise ModelOutputError("unknown_field", "引述条目包含未知字段")
        if keys != {"ref", "quote"}:
            raise ModelOutputError("missing_field", "引述条目缺少 ref 或 quote")
        ref = _parse_ref(raw.get("ref"))
        if ref in quotes:
            # 同一编号重复定义（即使内容相同）属于冲突定义，一律拒绝。
            raise ModelOutputError("duplicate_quote", "同一引用编号出现多条引述")
        quote = _require_str(raw.get("quote")).replace("\r\n", "\n")
        if not quote.strip():
            raise ModelOutputError("empty_quote", "引述不能为空或纯空白")
        if len(quote) > quote_limit:
            raise ModelOutputError("quote_too_long", "引述超过单条长度上限")
        item = evidence.item_by_ref(ref)
        if item is None:
            raise ModelOutputError("unknown_reference", "引述引用了本次证据范围之外的编号")
        # 只允许统一 CRLF：不删空格、不改大小写、不改数字、不删标点。
        if quote not in item.text.replace("\r\n", "\n"):
            raise ModelOutputError("quote_not_found", "引述不是对应证据正文的连续子串")
        quotes[ref] = quote
    return quotes


def validate_model_output(raw: str, evidence: EvidencePack, *, task_protocol: bool = False) -> ValidatedOutput:
    """完整校验模型输出；任何偏差抛出 ModelOutputError（稳定原因码）。"""
    payload = parse_model_output(raw)
    missing_raw = payload.pop("missing_points", []) if task_protocol else []
    _require_keys(payload)

    status = _require_str(payload.get("status"))
    if status not in (_ALLOWED_STATUS | {"partial"} if task_protocol else _ALLOWED_STATUS):
        raise ModelOutputError("bad_status", "status 取值不在允许范围内")

    total_chars = [0]
    conclusion = [_parse_fact(item, evidence=evidence, total_chars=total_chars)
                  for item in _require_list(payload.get("conclusion"))]
    explanation = [_parse_fact(item, evidence=evidence, total_chars=total_chars)
                   for item in _require_list(payload.get("explanation"))]
    if len(conclusion) + len(explanation) > (20 if task_protocol else MAX_FACT_ITEMS):
        raise ModelOutputError("facts_too_many", "事实条目总数超过上限")
    questions = _parse_questions(payload.get("clarification_questions"), total_chars=total_chars)
    missing_points = []
    for value in _require_list(missing_raw):
        text = _require_str(value)
        if not text.strip() or len(text) > 500:
            raise ModelOutputError("text_too_long", "缺项说明为空或超过长度上限")
        total_chars[0] += len(text)
        missing_points.append(text)
    if len(missing_points) > 8 or total_chars[0] > MAX_TOTAL_TEXT_CHARS:
        raise ModelOutputError("text_too_long", "缺项说明或总文字超过上限")
    if (status == "partial") != bool(missing_points):
        raise ModelOutputError("status_conflict", "部分回答必须明确列出缺项，其他状态不得列缺项")

    if status in {"answered", "partial"}:
        if not conclusion:
            raise ModelOutputError("empty_conclusion", "answered 至少需要一条结论事实")
        if questions:
            raise ModelOutputError("status_conflict", "answered 不允许包含澄清问题")
    elif status == "clarification_needed":
        if conclusion:
            raise ModelOutputError("status_conflict", "clarification_needed 的 conclusion 必须为空")
        if not explanation:
            raise ModelOutputError("empty_explanation", "clarification_needed 需要带引用的澄清原因")
        if not 1 <= len(questions) <= 2:
            raise ModelOutputError("question_count", "澄清问题必须是 1 到 2 个")
    else:  # insufficient_evidence
        if conclusion or explanation or questions:
            raise ModelOutputError("status_conflict", "insufficient_evidence 的四个列表必须为空")

    quotes = _parse_quotes(payload.get("evidence_quotes"), evidence=evidence,
                           quote_limit=600 if task_protocol else MAX_QUOTE_CHARS, allow_spans=task_protocol)
    if status == "insufficient_evidence" and quotes:
        raise ModelOutputError("status_conflict", "insufficient_evidence 的引述列表必须为空")

    result = ValidatedOutput(status=status, conclusion=conclusion, explanation=explanation,
                             clarification_questions=questions, quotes=quotes, missing_points=missing_points)
    if status != "insufficient_evidence":
        used = result.used_refs()
        if not used:
            raise ModelOutputError("empty_reference_list", "回答没有引用任何证据编号")
        missing = used - set(quotes.keys())
        if missing:
            raise ModelOutputError("missing_quote", "被使用的编号缺少对应引述")
        unused = set(quotes.keys()) - used
        if unused:
            raise ModelOutputError("unused_quote", "存在未被任何事实使用的引述")
    return result


def validate_user_condition_boundary(output: ValidatedOutput, evidence: EvidencePack,
                                     *, question: str) -> None:
    """拒绝把明确复述的用户条件混入带文档引用的事实条目。

    只检查至少四字符的成对引号内容：它逐字来自本次问题、却不存在于本条
    refs 的任一原文时，不能让文档编号承担用户原话的来源。含中文且不含数值的
    短语允许仅比较中文相邻的排版空白，避免OCR题名被误认成用户经历；不合并
    拉丁词间空格，不改数字、标点或大小写。此比较不改输出，也不适用于quote
    连续原文校验。短术语、书名号、
    未加引号的转述与 clarification_questions 不在此确定性检查范围内。
    这不是数字或语义校验；同一原句也确实出现在当前引用的资料中时仍可使用。
    不改写输出、不补引用、不追加模型调用，失败交给服务层返回安全错误。
    """
    quoted_user_text = re.compile(
        r'“([^“”\r\n]{4,})”|‘([^‘’\r\n]{4,})’|'
        r'「([^「」\r\n]{4,})」|『([^『』\r\n]{4,})』|"([^"\r\n]{4,})"')
    normalized_question = question.replace("\r\n", "\n")
    from app.rag_phrase_match import source_phrase_match,is_question_framing_term
    for fact_index, fact in enumerate(output.conclusion + output.explanation):
        if output.citation_bindings:
            # v4的refs是本次引述编号，不能误当成输入片段编号；只读本条所选证据。
            from app.rag_extract_context import merge_ranges
            by_source={}
            for ref in fact.refs:
                binding=output.citation_bindings[ref]
                by_source.setdefault(binding['source_ref'],[]).append([binding['char_start'],binding['char_end']])
            # 只合并本事实在同一原块内相接/重叠的真实范围；不拼接不同块或缺口。
            cited_texts=[evidence.item_by_ref(source_ref).text[a:b] for source_ref,ranges in by_source.items()
                for a,b in merge_ranges(ranges)]
        else:
            cited_texts = [item.text.replace("\r\n", "\n") for ref in fact.refs
                           if (item := evidence.item_by_ref(ref)) is not None]
        for match in quoted_user_text.finditer(fact.text):
            quoted = next(value for value in match.groups() if value is not None)
            if quoted in normalized_question and not any(source_phrase_match(quoted, text) for text in cited_texts):
                if is_question_framing_term(quoted,fact):
                    continue
                raise ModelOutputError("mixed_user_source",
                    "文档事实条目混入仅由用户问题提供的原句",
                    location=dict(fact_index=fact_index, reference_count=len(fact.refs)))


def render_markdown(output: ValidatedOutput) -> str:
    """由**已校验结构**生成用户可见 Markdown。

    引用标记只能由后端按 refs 生成，例如“事实。[1][2]”：
    不把引用统一堆在末尾，也不展示未经校验的原始模型输出。
    """
    if output.presentation is not None:
        from app.rag_presentation import render_answer
        return render_answer(output)
    def marker(refs: list[int]) -> str:
        return "".join(f"[{ref}]" for ref in sorted(refs))

    def fact_line(fact: ValidatedFact) -> str:
        # 模型条目内的换行不能拆出没有引用的独立段落，引用始终跟随同一条目。
        from app.rag_answer_contract import fact_display_text
        text = " ".join(fact_display_text(fact).splitlines())
        return f"- {text}{marker(fact.refs)}"

    lines: list[str] = []
    if output.status in {"answered", "partial"}:
        lines.append("## 结论")
        for fact in output.conclusion:
            lines.append(fact_line(fact))
        if output.explanation:
            lines.append("")
            lines.append("## 依据与说明")
            for fact in output.explanation:
                lines.append(fact_line(fact))
        if output.missing_points:
            lines.extend(["", "## 本次尚缺少的依据"])
            lines.extend("- " + " ".join(point.splitlines()) for point in output.missing_points)
    elif output.status == "clarification_needed":
        from app.rag_prompts import CLARIFICATION_LEAD

        lines.append(CLARIFICATION_LEAD)
        for position, question in enumerate(output.clarification_questions, start=1):
            lines.append(f"{position}. {question}")
        if output.explanation:
            lines.append("")
            lines.append("## 为什么需要这些信息")
            for fact in output.explanation:
                lines.append(fact_line(fact))
    # insufficient_evidence 的正文由后端固定兜底文本提供，不在这里生成。
    return "\n".join(lines).strip()


__all__ = [
    "MAX_FACT_ITEMS",
    "MAX_FACT_TEXT_CHARS",
    "MAX_QUESTION_CHARS",
    "MAX_QUOTE_CHARS",
    "MAX_TOTAL_TEXT_CHARS",
    "ModelOutputError",
    "ValidatedFact",
    "ValidatedOutput",
    "parse_model_output",
    "render_markdown",
    "validate_model_output",
    "validate_user_condition_boundary",
]
