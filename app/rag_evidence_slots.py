"""从问题与真实章节建立有限证据槽位；只是装入线索，不是答案充分性证明。"""
from __future__ import annotations

import re
from hashlib import sha256

from app.retrieval_planning import _bm25, _heading_views, _is_label, _search_text

SLOT_PLAN_VERSION = "question-source-slots-v5"


def evidence_slots(question: str, pool: list[dict], *, query_plan: dict | None = None) -> list[dict]:
    """无额外模型调用，不读取黄金答案或文件名；页码仅从类型化来源匹配。

    章节槽保留开头解释与后续正文两个不同候选；案例槽只用用户点名的对象。
    此处不把标题或数量相同解释成概念对应关系。
    """
    question = _search_text(question)
    from app.query_planning import plan_query, REQUIREMENTS
    from app.rag_math_evidence import formula_info, bad_glyphs
    from app.rag_slot_ranges import matching_ranges
    plan = query_plan or plan_query(question)
    headings, _ = _heading_views(pool)
    texts = [_search_text(hit['text']) for hit in pool]
    formulas=[formula_info(hit['text'],hit.get('chunk_type')) for hit in pool]
    eligible = [i for i,hit in enumerate(pool) if (not _is_label(hit) or formulas[i]['state']=='readable_expression')
        and ('formula_check' not in plan['facets'] or formulas[i]['state'] not in {'empty_formula','unreadable_expression'})]
    slots = []

    def add(kind, query, indices, *, opening=False,pattern=None,ranges=None):
        if not indices or len(slots) >= 64:
            return
        local = dict(_bm25(query, [texts[i] for i in indices]))
        ordered = sorted(range(len(indices)), key=lambda j: (-local.get(j, 0), len(texts[indices[j]]), indices[j]))
        ranked = [indices[j] for j in ordered]
        # 实际章节开头往往给机制/定义，后续才有问题点名的案例，保留两种用途。
        if opening:
            ranked = list(dict.fromkeys([indices[0], *ranked]))
        selected=ranked[:8 if opening else 3]
        slots.append({'id': f'slot-{len(slots) + 1}', 'kind': kind,
                      'candidate_ids': [pool[i]['chunk_id'] for i in selected],
                      'candidate_ranges':{pool[i]['chunk_id']:(ranges.get(pool[i]['chunk_id'],[]) if ranges is not None else
                          matching_ranges(pool[i]['text'],pattern) if pattern else [[0,len(pool[i]['text'])]]) for i in selected},
                      'range_kind':'anchor' if pattern or ranges is not None else 'passage',
                      'preferred_count': min(2, len(ranked)), 'semantic_support_verified': False})

    names = list(dict.fromkeys(re.findall(r'《([^》\n]{2,60})》', question)))[:6]
    from app.pdf_page_labels import requested_page_slots
    for request in requested_page_slots(question, pool):
        indices = [i for i in request['indices'] if i in eligible]
        prior = len(slots)
        add('source_page', question, indices)
        if len(slots) == prior:
            slots.append(dict(id=f'slot-{len(slots)+1}',kind='source_page',candidate_ids=[],candidate_ranges={},
                range_kind='passage',preferred_count=1,semantic_support_verified=False))
        slots[-1].update(key='page-'+request['kind']+'-'+request['value'],scope='document')
    if len(names) >= 2 or re.search(r'对比|比较|分别', question):
        for name in names:
            prior = len(slots)
            add('named_case', question, [i for i in eligible if name in texts[i]],pattern=re.escape(name))
            if len(slots)==prior:
                slots.append(dict(id=f'slot-{len(slots)+1}',kind='named_case',candidate_ids=[],candidate_ranges={},
                    range_kind='anchor',preferred_count=1,semantic_support_verified=False))
            slots[-1].update(key='case-'+sha256(name.encode()).hexdigest()[:16],scope='global')

    requested = set(re.findall(r'第\s*([1-9]\d*)\s*节', question))
    groups = {}
    roots = {}
    for i in eligible:
        tail = headings[i].split(' / ')[-1]
        number = re.match(r'\s*([1-9]\d*)(?:\.([1-9]\d*))?(?:\D|$)', tail)
        if number and number[1] in requested:
            target = groups if number[2] else roots
            key = number[1] + ('.' + number[2] if number[2] else '')
            target.setdefault(key, []).append(i)
    for key, indices in list(groups.items())[:8]:
        add('explicit_subsection', question, indices, opening=True)
    for key, indices in list(roots.items())[:8]:
        if not any(group.startswith(key + '.') for group in groups):
            add('explicit_section', question, indices, opening=True)

    if re.search(r'为什么|对比|是否符合|主张|观点|失效|因此', question):
        indices = [i for i in eligible if re.search(r'结论|结语|总结|conclusions?', headings[i], re.I)]
        add('author_conclusion', question, indices, opening=True)
    from app.rag_numeric_roles import source_number_tokens,source_number_mentions
    for number in sorted(source_number_tokens(question))[:8]:
        prior=len(slots)
        matches={pool[i]['chunk_id']:[[v['start'],v['end']] for v in source_number_mentions(pool[i]['text']) if v['value']==number] for i in eligible}
        add('numeric_source',question,[i for i in eligible if matches[pool[i]['chunk_id']]],ranges=matches)
        if len(slots)==prior:
            slots.append(dict(id=f'slot-{len(slots)+1}',kind='numeric_source',candidate_ids=[],preferred_count=1,
                semantic_support_verified=False))
        slots[-1].update(key='numeric-'+number,scope='global')
    if len(plan['subquestions']) > 1:
        for n, part in enumerate(plan['subquestions'], 1):
            ranked = _bm25(part, [texts[i] for i in eligible])
            indices = [eligible[j] for j, score in ranked[:3] if score > 0]
            prior = len(slots)
            from app.retrieval_planning import terms
            words=[re.escape(t) for t in set(terms(part)) if len(t)>=2]
            add('question_part', part, indices,pattern='|'.join(sorted(words)) or re.escape(part))
            if len(slots) == prior:
                slots.append(dict(id=f'slot-{len(slots)+1}',kind='question_part',candidate_ids=[],preferred_count=1,
                    semantic_support_verified=False))
            slots[-1].update(key=f'question-part-{n}',scope='global')
    for requirement in plan['requirements']:
        _, pattern, field = REQUIREMENTS[requirement]
        indices = [i for i in eligible if re.search(pattern, headings[i] if field == 'heading' else texts[i], re.I)]
        if requirement=='formula_source':indices=[i for i in eligible if formulas[i]['state']=='readable_expression']
        if requirement=='formula_explanation':indices=[i for i in indices if not bad_glyphs(pool[i]['text'])]
        if field == 'body':
            indices = [i for i in eligible if not re.search(r'摘要|abstract|参考文献|references|结论|结语|conclusion', headings[i], re.I)]
            # 综述正文轮换各节，不能只拿与“这份文档”词面相近的开头几块。
            by_section = {}
            for i in indices: by_section.setdefault(headings[i] or f'block-{i}', []).append(i)
            for n, group in enumerate(list(by_section.values())[:12], 1):
                add('overview_section', question, group, opening=True)
                slots[-1].update(key=f'overview-section-{n}', scope='document')
            if indices: continue
        if field not in ('heading', 'body'):
            # 通用标志不能把无关公式/条件当成答案；优先有实际问题词面命中的范围。
            lexical = dict(_bm25(question, [texts[i] for i in indices]))
            relevant = [indices[j] for j in lexical]
            indices = relevant
        prior = len(slots)
        add(requirement, question, indices, opening=field == 'heading',
            pattern=pattern if field=='text' else None,
            ranges={pool[i]['chunk_id']:formulas[i]['ranges'] for i in indices} if requirement=='formula_source' else None)
        if len(slots) == prior:
            slots.append(dict(id=f'slot-{len(slots)+1}',kind=requirement,candidate_ids=[],preferred_count=1,
                semantic_support_verified=False))
        slots[-1].update(key=requirement, scope='global')
    # 纯事实问答不强造多跳结构；任务要求即使无候选也保留空槽供诊断。
    return slots if len(slots) >= 2 or plan['requirements'] or any(s['kind'] in {'numeric_source','named_case','source_page'} for s in slots) else []


def slot_hints(slots: list[dict], selected_ids: set[str], admitted=None) -> dict:
    from app.rag_slot_ranges import contains_anchor,SLOT_RANGE_VERSION
    entries=[]
    for slot in slots:
        present=[i for i in slot['candidate_ids'] if i in selected_ids]
        covered=[i for i in present if admitted is None or contains_anchor(admitted.get(i,[]),slot.get('candidate_ranges',{}).get(i,[]))]
        entries.append(dict(id=slot['id'],kind=slot['kind'],selected_chunk_ids=covered,candidate_count=len(slot['candidate_ids']),
            selected_candidate_count=len(present),
            state='admitted' if covered else 'outside_admitted_ranges' if present else 'not_admitted' if slot['candidate_ids'] else 'no_candidate'))
    return {'version': SLOT_PLAN_VERSION, 'heuristic_only': True, 'semantic_completeness_verified': False,
            'range_policy':SLOT_RANGE_VERSION,'slots': entries, 'unfilled_count': sum(not e['selected_chunk_ids'] for e in entries)}
