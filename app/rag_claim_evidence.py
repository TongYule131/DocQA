"""逐论断选择连续原文；只验证身份、范围和协议，不宣称语义蕴含已通过。"""
from __future__ import annotations

from dataclasses import replace
import json

from app.quote_spans import context_segments, quote_choice_ranges
from app.rag_context import EvidencePack
from app.rag_validation import ModelOutputError, parse_model_output

CLAIM_TYPES = {"direct", "synthesis", "inference"}


def _fail(reason="claim_evidence_invalid"):
    raise ModelOutputError(reason)


def validate_unit_output(raw,evidence,*,question,declare_parts=False,derive_refs=False,expected_parts=None):
    """v5直接选择生成前公布的原句单位；服务器只展开选项，不修复输出。"""
    from app.rag_extract_context import quote_units
    payload=parse_model_output(raw)
    expected={'status','conclusion','explanation','clarification_questions','missing_points','answer_parts'}
    if set(payload)!=expected:_fail('claim_evidence_invalid')
    selected={};declared=set();part_indices={};fact_index=0
    if declare_parts:
        from app.rag_answer_contract import question_parts
        part_indices={part['part_id']:[] for part in (question_parts(question) if expected_parts is None else expected_parts)}
    for name in ('conclusion','explanation'):
        if not isinstance(payload[name],list):_fail()
        for fact in payload[name]:
            if not isinstance(fact,dict) or not isinstance(fact.get('evidence'),list) or not 1<=len(fact['evidence'])<=8:_fail()
            refs=fact.get('refs')
            if derive_refs:
                if 'refs' in fact:_fail()
            elif not isinstance(refs,list) or not refs or any(type(ref) is not int for ref in refs) or len(set(refs))!=len(refs):_fail()
            if declare_parts:
                parts=fact.pop('part_ids',None)
                if (not isinstance(parts,list) or not parts or any(not isinstance(p,str) or p not in part_indices for p in parts)
                        or len(set(parts))!=len(parts)):_fail('coverage_mismatch')
                for part in parts:part_indices[part].append(fact_index)
            fact_index+=1
            parts=[];seen=set()
            for choice in fact['evidence']:
                if not isinstance(choice,dict) or set(choice)!={'ref','unit'}:_fail()
                ref,unit=choice['ref'],choice['unit']
                if type(ref) is not int or type(unit) is not int or (ref,unit) in seen:_fail()
                seen.add((ref,unit));item=evidence.item_by_ref(ref)
                if item is None:_fail('quote_not_in_admitted_input')
                if ref not in selected:
                    selected[ref]=(item.unit_choices if derive_refs else
                        quote_units(item.text,item.read_ranges,strict=item.segment_policy=='sentence-segments-v2'))
                if selected[ref] is None:_fail('quote_not_in_admitted_input')
                options=selected[ref]
                ranges=options.get(str(unit))
                if not ranges:_fail('quote_not_in_admitted_input')
                for bounds in ranges:
                    source_ref,span,first,last=bounds if derive_refs else (ref,*bounds)
                    part=dict(ref=source_ref,span=span,start_segment=first,end_segment=last)
                    if part not in parts:parts.append(part)
                    declared.add((source_ref,span))
            if derive_refs:fact['refs']=sorted({part['ref'] for part in parts})
            elif {ref for ref,unit in seen}!=set(fact.get('refs',[])):_fail()
            fact['evidence']=parts
    payload['evidence_quotes']=[dict(ref=ref,span=span) for ref,span in sorted(declared)]
    if declare_parts:
        if not isinstance(payload.get('answer_parts'),list):_fail('coverage_mismatch')
        for part in payload['answer_parts']:
            if (not isinstance(part,dict) or set(part)!={'part_id','state','missing_detail'}
                    or part.get('part_id') not in part_indices):_fail('coverage_mismatch')
            part['fact_indices']=part_indices[part['part_id']]
    return validate_claim_output(json.dumps(payload,ensure_ascii=False),evidence,question=question,
        max_supports=48 if derive_refs else 24,range_limit=80 if derive_refs else 40,expected_parts=expected_parts)


def validate_claim_output(raw: str, evidence: EvidencePack, *, question: str,max_supports=8,range_limit=40,expected_parts=None):
    """v4模型只选择服务器窗口/小段，不自行填写quote、偏移、证据ID或坐标。"""
    from app.rag_answer_contract import validate_task_output

    payload = parse_model_output(raw)
    windows = payload.get("evidence_quotes")
    if not isinstance(windows, list) or len(windows) > range_limit:
        _fail()
    declared = {}
    for window in windows:
        if not isinstance(window, dict) or set(window) != {"ref", "span"}:
            _fail()
        ref, span = window["ref"], window["span"]
        if type(ref) is not int or type(span) is not int or (ref, span) in declared:
            _fail()
        item = evidence.item_by_ref(ref)
        ranges = quote_choice_ranges(item.text) if item else []
        if not 1 <= span <= len(ranges):
            _fail()
        start, end = ranges[span - 1]
        text = item.text.replace("\r\n", "\n")[start:end]
        from app.rag_extract_context import strict_segments, visible_segments
        segments = strict_segments(text) if item.segment_policy == 'sentence-segments-v2' else context_segments(text)
        visible = {number: positions for number,_,positions in visible_segments(item.text,item.read_ranges,
                    strict=item.segment_policy == 'sentence-segments-v2')}
        if span not in visible: _fail('quote_not_in_admitted_input')
        declared[ref, span] = (start, segments, visible[span])

    facts = []
    bindings, used_windows = {}, set()
    for name in ("conclusion", "explanation"):
        entries = payload.get(name)
        if not isinstance(entries, list) or len(entries) > 20:
            _fail()
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {"text", "refs", "context", "claim_type", "evidence"}
                    or not isinstance(entry["claim_type"], str) or entry["claim_type"] not in CLAIM_TYPES):
                _fail("claim_type_missing_or_invalid")
            refs, supports, contexts = entry["refs"], entry["evidence"], entry["context"]
            if (not isinstance(refs, list) or not refs or any(type(v) is not int for v in refs)
                    or len(set(refs)) != len(refs) or not isinstance(supports, list)
                    or not 1 <= len(supports) <= max_supports or not isinstance(contexts, list)):
                _fail()
            local, keys = [], set()
            for support in supports:
                if not isinstance(support, dict) or set(support) != {"ref", "span", "start_segment", "end_segment"}:
                    _fail()
                ref, span, first, last = (support[k] for k in ("ref", "span", "start_segment", "end_segment"))
                if any(type(v) is not int for v in (ref, span, first, last)) or ref not in refs:
                    _fail()
                window = declared.get((ref, span))
                if window is None or not 1 <= first <= last <= len(window[1]):
                    _fail()
                base, segments, visible = window
                if any(n not in visible for n in range(first,last+1)): _fail('quote_not_in_admitted_input')
                start = base + sum(map(len, segments[:first - 1]))
                end = base + sum(map(len, segments[:last]))
                key = ref, start, end
                if key in keys:
                    _fail()
                keys.add(key)
                used_windows.add((ref, span))
                bindings[key] = dict(source_ref=ref, char_start=start, char_end=end)
                local.append((key, support))
            if {s[1]["ref"] for s in local} != set(refs):
                _fail()
            facts.append((entry, local))
    if used_windows != set(declared) or len(bindings) > range_limit:
        _fail()

    # 引用号只用于本次展示。稳定身份在服务层依据原版本、块和范围另行计算。
    assigned = {key: index for index, key in enumerate(sorted(bindings), 1)}
    items, quotes = [], []
    for key, reference_id in assigned.items():
        source_ref, start, end = key
        item = evidence.item_by_ref(source_ref)
        quote = item.text.replace("\r\n", "\n")[start:end]
        if not quote.strip():
            _fail()
        items.append(replace(item, reference_id=reference_id, text=quote))
        quotes.append(dict(ref=reference_id, quote=quote))
    translated = dict(payload, evidence_quotes=quotes)
    for name in ("conclusion", "explanation"):
        translated[name] = []
    for index, (entry, local) in enumerate(facts):
        contexts = []
        for context in entry["context"]:
            if not isinstance(context, dict) or set(context) != {"kind", "ref", "span", "segment"}:
                _fail()
            ref, span, segment = (context[k] for k in ("ref", "span", "segment"))
            if any(type(v) is not int for v in (ref, span, segment)):
                _fail()
            matches = [(key, s) for key, s in local if s["ref"] == ref and s["span"] == span
                       and s["start_segment"] <= segment <= s["end_segment"]]
            if not matches:
                _fail("context_not_in_claim_evidence")
            key = min(key for key, _ in matches)
            contexts.append(dict(kind=context["kind"], ref=assigned[key], text=declared[ref, span][1][segment - 1]))
        name = "conclusion" if index < len(payload["conclusion"]) else "explanation"
        translated[name].append(dict(text=entry["text"], refs=[assigned[key] for key, _ in local], context=contexts))
    scoped = EvidencePack(items=items, candidates=items, index_id=evidence.index_id,
                          parse_version_id=evidence.parse_version_id, truncated=evidence.truncated,
                          context_chars=evidence.context_chars)
    from app.rag_extract_context import merge_ranges
    joined=[]
    for entry,local in facts:
        own={}
        for key,support in local:own.setdefault(key[0],[]).append([key[1],key[2]])
        joined.append([evidence.item_by_ref(ref).text[a:b] for ref,ranges in own.items() for a,b in merge_ranges(ranges)])
    output = validate_task_output(json.dumps(translated, ensure_ascii=False), scoped, question=question,
                                 contract_version="fact-context-parts-v3",_claim_sources=joined,
                                 _section_headings=[item.heading_path for item in evidence.items],_expected_parts=expected_parts)
    for fact, (entry, _) in zip(output.conclusion + output.explanation, facts):
        fact.claim_type = entry["claim_type"]
    output.citation_bindings = {assigned[key]: value for key, value in bindings.items()}
    return output
