"""任务式事实限定与子问声明。只做可判定检查，不充当一般语义判官。"""
from __future__ import annotations

import json
import re

from app.rag_validation import ModelOutputError, parse_model_output, validate_model_output

ANSWER_CONTRACT_VERSION = "fact-context-parts-v7"
SUPPORTED_CONTRACT_VERSIONS = {"fact-context-parts-v1", "fact-context-parts-v2", "fact-context-parts-v3", "fact-context-parts-v4", "fact-context-parts-v5", "fact-context-parts-v6", ANSWER_CONTRACT_VERSION}
CONTEXT_LABELS = {"subject": "对象", "period": "期间", "unit": "单位",
                  "scope": "范围", "condition": "限定", "role": "用途"}
# 紧接数字或百分号的连字符属于区间分隔，中文/英文后的一元正负号仍须保留。
_NUMBERS = re.compile(r"(?<![\d%％])[+\-−＋－]?\d+(?:[.,]\d+)*(?:\s*[%％])?|\d+(?:[.,]\d+)*(?:\s*[%％])?")


def question_parts(question: str) -> list[dict]:
    """按原标点划分，不补造正式定义等前提；长问题的余部保留在最后一项。"""
    parts = [v.strip() for v in re.findall(r"[^？?；;]+[？?；;]?", question) if v.strip()]
    if len(parts) > 8:
        parts = parts[:7] + ["".join(parts[7:])]
    return [{"part_id": f"p{i}", "text": text} for i, text in enumerate(parts or [question], 1)]


def _fail(reason):
    raise ModelOutputError(reason)


def _tokens(value):
    # 定位编号不是统计值。仅统一明确的三位分组，不合并任意OCR数字间空格。
    value=re.sub(r'第\s*\d+(?:\.\d+)*\s*[章节页段]', '', value)
    value=re.sub(r'第\s*\d+(?:\.\d+)+(?:[、和及与,，]\s*\d+(?:\.\d+)+)*(?:节)?','',value)
    value=re.sub(r'((?:原文|论文)在[“"‘])\d+(?:\.\d+)*\s+(?=[^”"’]{2,80}[”"’](?:开篇|中|部分))',r'\1',value)
    value=re.sub(r'(?<![\d.])\d{1,3}(?:[ \u00a0]\d{3})+(?:\.\d+)?(?!\d|[ \u00a0]\d)',
                 lambda m:re.sub(r'[ \u00a0]','',m.group()),value)
    return {re.sub(r"\s+(?=[%％])", "", v) for v in _NUMBERS.findall(value)}


def _context_numbers(value):
    # 概念列举的个数和带版本号的文字标识不是金额/测量；原文数值仍由_tokens核对。
    value=re.sub(r'\d+世纪(?:初|末|中叶)?','',value)
    value=re.sub(r'\d+\s*(?:个|种)?(?:层面|层级|机制|维度|类别|方面|要素)(?![\u3400-\u9fff]*(?:元|人|天|小时|件))','',value)
    # 只有明确称为编号/版本/许可等文字标识时才免测量context；USD10-20仍是金额。
    pattern=r'(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)+(?![A-Za-z0-9])'
    value=re.sub(pattern,lambda m:'' if re.search(r'(?:许可(?:证)?(?:标识)?|标识|编号|版本|合成|license|version)\s*(?:为|是|[:：])?\s*$',value[:m.start()],re.I) else m.group(),value)
    return _tokens(value)


def _without_known_section_locators(value,evidence,section_headings=None):
    """仅识别真实输入标题中的编号，且要求明确的标题/章节关系语法。

    元数据可提供章节地址，不能拿其数字为金额或比例背书；不直接从所有小数中
    删除标题编号。范围判断不修改事实文字或原文引述。
    """
    headings={}
    for heading in (section_headings if section_headings is not None else [item.heading_path for item in evidence.items]):
        for part in re.split(r'\s+/\s+',heading or ''):
            match=re.match(r'\s*(\d+(?:\.\d+)+)\s+(.+)',part)
            if match:headings.setdefault(match[1],[]).append(re.sub(r'\s+','',match[2]))
    if not headings:return value
    def nested(match):
        parent,child=match[1],match[2]
        return match.group()[:-len(child)] if child in headings and child.startswith(parent+'.') else match.group()
    # “第3节3.1虽然……”是显式嵌套章节地址；带金额/单位后缀的3.1仍须核对。
    value=re.sub(r'第(\d+)[章节](?:的)?(\d+(?:\.\d+)+)(?![\d.]|元|万|亿|件|人|天|小时|年|月|日|[%％])',nested,value)
    pattern=r'(?<![\d.])(\d+(?:\.\d+)+)的[“‘"]([^”’"]{2,80})[”’"]'
    def titled(match):
        title=re.sub(r'\s+','',match[2])
        return '的“'+match[2]+'”' if any(title in h for h in headings.get(match[1],[])) else match.group()
    value=re.sub(pattern,titled,value)
    # 只处理“推出3.2、3.3与另两个层面配对”这类明确章节关系，不处理数值列表。
    pattern=r'((?:推出|对应|配对|小节为|章节为)\s*)((?:\d+(?:\.\d+)+[、,，]\s*)*\d+(?:\.\d+)+)(?=(?:与|和|及)(?:另|其他)[^。；，]{0,16}(?:层面|机制|章节|小节))'
    value=re.sub(pattern,lambda m:m[1] if all(n in headings for n in re.findall(r'\d+(?:\.\d+)+',m[2])) else m.group(),value)
    return value


# 旧离线调用默认按原v3读取；生产必须显式传入任务冻结的协议版本，禁止失败后降级。
def validate_task_output(raw, evidence, *, question, contract_version="fact-context-parts-v3", _claim_sources=None,_section_headings=None):
    if not isinstance(contract_version, str) or contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        _fail("unsupported_contract_version")
    if contract_version == ANSWER_CONTRACT_VERSION:
        from app.rag_claim_evidence import validate_unit_output
        return validate_unit_output(raw,evidence,question=question,declare_parts=True,derive_refs=True)
    if contract_version=='fact-context-parts-v6':
        from app.rag_claim_evidence import validate_unit_output
        return validate_unit_output(raw,evidence,question=question,declare_parts=True)
    if contract_version == 'fact-context-parts-v5':
        from app.rag_claim_evidence import validate_unit_output
        return validate_unit_output(raw,evidence,question=question)
    if contract_version == "fact-context-parts-v4":
        from app.rag_claim_evidence import validate_claim_output
        return validate_claim_output(raw, evidence, question=question)
    payload = parse_model_output(raw)
    parts = payload.pop("answer_parts", None)
    if not isinstance(parts, list):
        _fail("coverage_missing")
    contexts = []
    for name in ("conclusion", "explanation"):
        entries = payload.get(name)
        if not isinstance(entries, list):
            _fail("bad_type")
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict) or "context" not in entry:
                _fail("fact_context_missing")
            context = entry["context"]
            if not isinstance(context, list) or len(context) > 12:
                _fail("bad_type")
            contexts.append(context)
            cleaned.append({key: value for key, value in entry.items() if key != "context"})
        payload[name] = cleaned
    # 只分离已定义的扩展字段；正文、引述和编号不修补，原有严格解析继续执行。
    output = validate_model_output(json.dumps(payload, ensure_ascii=False), evidence, task_protocol=True)
    facts = output.conclusion + output.explanation
    total = sum(len(f.text) for f in facts) + sum(map(len, output.clarification_questions + output.missing_points))
    for fact_index,(fact, context) in enumerate(zip(facts, contexts)):
        seen = set()
        accepted = []
        for binding in context:
            if not isinstance(binding, dict):
                _fail("bad_type")
            if set(binding) == {'kind', 'ref', 'segment'} and contract_version == "fact-context-parts-v3":
                from app.quote_spans import context_segments
                ref, segment = binding['ref'], binding['segment']
                if type(ref) is not int or ref not in fact.refs or type(segment) is not int:
                    _fail('context_scope_mismatch')
                options = context_segments(output.quotes[ref])
                if not 1 <= segment <= len(options):
                    _fail('context_scope_mismatch')
                binding = dict(kind=binding['kind'], ref=ref, text=options[segment - 1])
                output.context_selection_count += 1
            elif set(binding) != {"kind", "text", "ref"}:
                _fail('bad_type')
            kind, text, ref = binding["kind"], binding["text"], binding["ref"]
            if (not isinstance(kind, str) or kind not in CONTEXT_LABELS or not isinstance(text, str)
                    or not text.strip() or len(text) > 120 or "[" in text or "]" in text
                    or type(ref) is not int or ref not in fact.refs):
                _fail("context_scope_mismatch")
            if text.replace("\r\n", "\n") not in output.quotes[ref]:
                _fail("context_not_in_quote")
            identity = kind, text, ref
            if identity in seen:
                _fail("context_scope_mismatch")
            seen.add(identity)
            accepted.append(dict(binding))
            total += len(text)
        numeric_text=_without_known_section_locators(fact.text,evidence,_section_headings)
        numbers = _tokens(numeric_text)
        context_numbers=_context_numbers(numeric_text)
        if context_numbers and not context:
            _fail("numeric_context_missing")
        sources=_claim_sources[fact_index] if _claim_sources is not None else [output.quotes[ref] for ref in fact.refs]
        available = set().union(*(_tokens(source) for source in sources))
        if not numbers <= available:
            _fail("number_not_in_quote")
        fact.context = accepted
    if total > 4000:
        _fail("text_too_long")
    expected = question_parts(question)
    if len(parts) != len(expected):
        _fail("coverage_mismatch")
    states, assigned, gaps = [], set(), []
    for entry, required in zip(parts, expected):
        if not isinstance(entry, dict) or set(entry) != {"part_id", "state", "fact_indices", "missing_detail"}:
            _fail("coverage_mismatch")
        allowed_states = {"answered", "missing_evidence", "needs_clarification"}
        if contract_version in {"fact-context-parts-v2", "fact-context-parts-v3"}:
            allowed_states.add("partial")
        if (entry["part_id"] != required["part_id"] or not isinstance(entry["state"], str)
                or entry["state"] not in allowed_states):
            _fail("coverage_mismatch")
        indices, detail = entry["fact_indices"], entry["missing_detail"]
        if (not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(facts) for i in indices)
                or len(set(indices)) != len(indices) or not isinstance(detail, str) or len(detail) > 500):
            _fail("coverage_mismatch")
        state = entry["state"]
        if state == "answered":
            if not indices or detail:
                _fail("coverage_mismatch")
        else:
            if (not detail.strip() or (state == "missing_evidence" and indices)
                    or (state == "partial" and not indices)):
                _fail("coverage_mismatch")
            # 只拦截明确强加的定义前提；不能据此认定所有比较题都可回答。
            if (re.search(r"区别|差异|区分|比较", question) and not re.search(r"定义|界定|术语含义", question)
                    and re.search(r"定义|界定", detail)):
                _fail("unsupported_requirement")
            gaps.append(detail)
        assigned.update(indices)
        states.append(state)
    if assigned != set(range(len(facts))):
        _fail("coverage_mismatch")
    expected_status = ("answered" if all(s == "answered" for s in states) else
                       "partial" if any(s in {"answered", "partial"} for s in states) else
                       "clarification_needed" if "needs_clarification" in states else "insufficient_evidence")
    if output.status != expected_status or (output.status == "partial" and output.missing_points != gaps):
        _fail("coverage_status_conflict")
    # partial的缺项已在missing_points计数；其他状态的分项原因也须受总预算约束。
    if output.status != "partial":
        total += sum(map(len, gaps))
    if total > 4000:
        _fail("text_too_long")
    output.answer_parts = [{**entry, "text": required["text"]} for entry, required in zip(parts, expected)]
    return output


def fact_display_text(fact):
    """显式展示模型声明且经原文核对的限定；不推断、补写模型没有提供的字段。"""
    text = fact["text"] if isinstance(fact, dict) else fact.text
    context = fact.get("context", []) if isinstance(fact, dict) else getattr(fact, "context", [])
    details = []
    for entry in context:
        entry = entry if isinstance(entry, dict) else entry.model_dump()
        if entry["text"] not in text:
            details.append(CONTEXT_LABELS[entry["kind"]] + "：" + entry["text"])
    kind = fact.get("claim_type", "unclassified") if isinstance(fact, dict) else getattr(fact, "claim_type", "unclassified")
    label = {"direct": "原文直述", "synthesis": "多处综合", "inference": "结构推断"}.get(kind)
    review = fact.get('support_review',{}) if isinstance(fact,dict) else getattr(fact,'support_review',{})
    note = ('（证据规则检查：需复核；语义待复核）' if review.get('status')=='needs_review'
            else '（证据规则检查通过；语义待复核）' if review.get('status')=='checks_passed' else '')
    return (f"【{label}】" if label else "") + text + ("（" + "；".join(details) + "）" if details else "") + note
