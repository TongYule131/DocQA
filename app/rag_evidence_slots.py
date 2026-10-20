"""从问题与真实章节建立有限证据槽位；只是装入线索，不是答案充分性证明。"""
from __future__ import annotations

import re

from app.retrieval_planning import _bm25, _heading_views, _is_label, _search_text

SLOT_PLAN_VERSION = "question-source-slots-v2"


def evidence_slots(question: str, pool: list[dict]) -> list[dict]:
    """无额外模型调用，不读取黄金答案、文件名或页码分支。

    章节槽保留开头解释与后续正文两个不同候选；案例槽只用用户点名的对象。
    此处不把标题或数量相同解释成概念对应关系。
    """
    question = _search_text(question)
    headings, _ = _heading_views(pool)
    texts = [_search_text(hit['text']) for hit in pool]
    eligible = [i for i, hit in enumerate(pool) if not _is_label(hit)]
    slots = []

    def add(kind, query, indices, *, opening=False):
        if not indices or len(slots) >= 16:
            return
        local = dict(_bm25(query, [texts[i] for i in indices]))
        ordered = sorted(range(len(indices)), key=lambda j: (-local.get(j, 0), len(texts[indices[j]]), indices[j]))
        ranked = [indices[j] for j in ordered]
        # 实际章节开头往往给机制/定义，后续才有问题点名的案例，保留两种用途。
        if opening:
            ranked = list(dict.fromkeys([indices[0], *ranked]))
        slots.append({'id': f'slot-{len(slots) + 1}', 'kind': kind,
                      'candidate_ids': [pool[i]['chunk_id'] for i in ranked[:8 if opening else 3]],
                      'preferred_count': min(2, len(ranked)), 'semantic_support_verified': False})

    names = list(dict.fromkeys(re.findall(r'《([^》\n]{2,60})》', question)))[:6]
    if len(names) >= 2 or re.search(r'对比|比较|分别', question):
        for name in names:
            add('named_case', question, [i for i in eligible if name in texts[i]])

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
    for key, indices in roots.items():
        if not any(group.startswith(key + '.') for group in groups):
            add('explicit_section', question, indices, opening=True)

    if re.search(r'为什么|对比|是否符合|主张|观点|失效|因此', question):
        indices = [i for i in eligible if re.search(r'结论|结语|总结|conclusions?', headings[i], re.I)]
        add('author_conclusion', question, indices, opening=True)
    # 纯事实问答不强造多跳结构，继续原排名/整块装入路径。
    return slots if len(slots) >= 2 else []


def slot_hints(slots: list[dict], selected_ids: set[str]) -> dict:
    entries = [{'id': slot['id'], 'kind': slot['kind'],
                'selected_chunk_ids': [i for i in slot['candidate_ids'] if i in selected_ids]}
               for slot in slots]
    return {'version': SLOT_PLAN_VERSION, 'heuristic_only': True, 'semantic_completeness_verified': False,
            'slots': entries, 'unfilled_count': sum(not e['selected_chunk_ids'] for e in entries)}
