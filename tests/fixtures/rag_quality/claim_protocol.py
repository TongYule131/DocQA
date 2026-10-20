"""旧合成响应的显式v4制版工具；仅升级格式，不修补错误事实、编号或引述。"""
import copy

from app.quote_spans import context_segments, quote_choices


def declare_parts(body,*,derive_refs=False):
    body=copy.deepcopy(body)
    for index,fact in enumerate(body['conclusion']+body['explanation']):
        fact['part_ids']=[p['part_id'] for p in body['answer_parts'] if index in p['fact_indices']]
    for part in body['answer_parts']:part.pop('fact_indices')
    if derive_refs:
        for fact in body['conclusion']+body['explanation']:fact.pop('refs')
    return body


def upgrade(body, sources, *, strict=False, units=True):
    from app.rag_extract_context import strict_segments
    splitter = strict_segments if strict else context_segments
    body = copy.deepcopy(body)
    windows = {}
    for quoted in body["evidence_quotes"]:
        ref = quoted["ref"]
        choices = quote_choices(sources[ref])
        if "span" in quoted:
            span = quoted["span"]
        else:
            assert quoted["quote"] in sources[ref]
            span = next(i for i, text in enumerate(choices, 1) if quoted["quote"] in text)
        windows[ref] = span
    body["evidence_quotes"] = [dict(ref=ref, span=span) for ref, span in windows.items()]
    for fact in body["conclusion"] + body["explanation"]:
        fact["claim_type"] = "direct"
        fact["evidence"] = [dict(ref=ref, span=windows[ref], start_segment=1,
            end_segment=len(splitter(quote_choices(sources[ref])[windows[ref] - 1]))) for ref in fact["refs"]]
        contexts = []
        for context in fact["context"]:
            ref, span = context["ref"], windows[context["ref"]]
            segments = splitter(quote_choices(sources[ref])[span - 1])
            if "segment" in context:
                selected = [context["segment"]]
            else:
                text = "".join(segments)
                start = text.index(context["text"])
                end, cursor, selected = start + len(context["text"]), 0, []
                for index, segment in enumerate(segments, 1):
                    if cursor < end and cursor + len(segment) > start:
                        selected.append(index)
                    cursor += len(segment)
            contexts.extend(dict(kind=context["kind"], ref=ref, span=span, segment=i) for i in selected)
        fact["context"] = contexts
    if units:
        from app.rag_extract_context import quote_units,passage_units
        body.pop('evidence_quotes')
        body.setdefault('missing_points',[])
        for fact in body['conclusion']+body['explanation']:
            # 仅供正向合成响应制版：引用完整既定原文，不改写事实、限定或编号。
            fact['evidence']=[dict(ref=ref,unit=int(unit)) for ref in fact['refs']
                for unit in (passage_units(sources[ref],None) if strict else quote_units(sources[ref],None,strict=False))]
    return declare_parts(body,derive_refs=True) if units else body
