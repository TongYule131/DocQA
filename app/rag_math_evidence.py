"""数学证据关联：核对表示是否可读，保全同一命题两端；不计算或认证公式正确。"""
from __future__ import annotations

from hashlib import sha256
import json
import re

from app.rag_numeric_roles import locate

MATH_EVIDENCE_VERSION = 'math-evidence-groups-v1'
_RELATION = re.compile(r'[=<>≤≥≠∈∉≈]|\\(?:leq?|geq?|neq|equiv|in)\b')
_OPERAND = re.compile(r'[A-Za-z0-9\u0370-\u03ff\u4e00-\u9fff]')
_LEFT_EDGE=re.compile(r'[A-Za-z0-9\u0370-\u03ff\u4e00-\u9fff}\])]$')
_RIGHT_EDGE=re.compile(r'^[A-Za-z0-9\u0370-\u03ff\u4e00-\u9fff\\{(\[+\-−]')
_EXPLANATION = re.compile(r'表明|说明|意味着|解释|高于|低于|增加|减少|上升|下降')
_DEFINITION = re.compile(r'其中|表示|代表|定义|系数|参数|满足|条件')


def bad_glyphs(text):
    return any(ch=='\ufffd' or '\ue000'<=ch<='\uf8ff' or ord(ch)<32 and not ch.isspace() for ch in text)


def formula_info(text, chunk_type=None):
    """返回真实原文范围。可读只指文字表示，未知上下标/符号意义仍须人工核对。"""
    typed=chunk_type=='formula'
    if not text.strip():
        return dict(state='empty_formula' if typed else 'not_formula',ranges=[],reason='missing_text' if typed else None)
    if re.search(r'</?[A-Za-z][^>]{0,80}>',text):
        return dict(state='unreadable_expression' if typed else 'not_formula',ranges=[],reason='markup_ambiguous' if typed else None)
    ranges=[];has_operator=False
    for match in re.finditer(r'[^。；;\n]+',text):
        part=match.group();relations=list(_RELATION.finditer(part))
        usable=[]
        for relation in relations:
            left=part[:relation.start()].rstrip(' $\t');right=part[relation.end():].lstrip(' $\t')
            left_ok=bool(_LEFT_EDGE.search(left) and _OPERAND.search(left))
            has_operator|=left_ok
            usable.append(left_ok and bool(_RIGHT_EDGE.search(right) and _OPERAND.search(right)))
        if any(usable):
            ranges.append([match.start(),match.end()])
        elif typed and re.search(r'\\(?:frac|sqrt|sum|int)\b|[+*/^∑∫]',part) and _OPERAND.search(part):
            ranges.append([match.start(),match.end()])
    formula=typed or has_operator
    reason=('unreadable_glyph' if bad_glyphs(text) else
            'unbalanced_braces' if text.count('{')!=text.count('}') else
            'incomplete_expression' if not ranges else None)
    state=('unreadable_expression' if reason else 'readable_expression') if formula else 'not_formula'
    return dict(state=state,ranges=ranges if state=='readable_expression' else [],reason=reason if formula else None)


def _statements(hit):
    return {(p.kind,tuple(p.values)) for p in locate(hit['text']) if p.kind in {'命题','推论','定理','引理'} and p.values}


def math_evidence(pool, question=''):
    """只关联固定版本、相同章节、近邻且明确引用同一命题的片段。

    组用于装入，绝不将不同片段合成一句quote，也不把相邻关系当条件适用认证。
    """
    info={h['chunk_id']:formula_info(h['text'],h.get('chunk_type')) for h in pool}
    statements=[_statements(h) for h in pool]
    requested=_statements({'text':question})
    groups=[];seen=set();linked=set()
    for n,hit in enumerate(pool):
        if info[hit['chunk_id']]['state']!='readable_expression':continue
        scope=lambda h:(h['document_id'],h.get('parse_version_id'),h.get('heading_path'))
        near=[i for i in range(max(0,n-4),min(len(pool),n+5)) if scope(pool[i])==scope(hit)]
        keys=statements[n]
        labels=[]
        if not keys:
            # 无命题标签的独立式子只能借用紧邻、唯一的显式标签；不跨章节猜测。
            anchors=[i for i in near if abs(i-n)<=1 and statements[i]]
            possible=set().union(*(statements[i] for i in anchors)) if anchors else set()
            if len(possible)==1:
                keys=possible;labels=[i for i in anchors if len(pool[i]['text'].strip())<=30]
        if not keys or requested and not keys&requested:continue
        explicit=[i for i in near if statements[i]&keys]
        explanation=[i for i in explicit if i!=n and _EXPLANATION.search(pool[i]['text'])
                     and not bad_glyphs(pool[i]['text'])]
        if not explanation:continue
        explanation=sorted(explanation,key=lambda i:(abs(i-n),i))[:2]
        definitions=[i for i in explicit if i!=n and i not in explanation and _DEFINITION.search(pool[i]['text'])
                     and not bad_glyphs(pool[i]['text'])]
        if not definitions:
            symbols=set(re.findall(r'\\[A-Za-z]+|(?<![A-Za-z_{])[A-Za-z\u0370-\u03ff](?![A-Za-z])',hit['text']))
            following=[i for i in near if 0<i-n<=2 and not statements[i]
                and re.match(r'\s*(?:其中|式中)',pool[i]['text']) and _DEFINITION.search(pool[i]['text'])
                and not bad_glyphs(pool[i]['text']) and any(re.search(re.escape(symbol)+r'(?![A-Za-z])',pool[i]['text']) for symbol in symbols)
                and not any(info[pool[j]['chunk_id']]['state']=='readable_expression' for j in range(n+1,i))]
            if len(following)==1:definitions=following
        definitions=sorted(definitions,key=lambda i:(abs(i-n),i))[:1]
        members=list(dict.fromkeys([n,*explanation,*definitions,*labels]))
        ids=[pool[i]['chunk_id'] for i in sorted(members)]
        required={hit['chunk_id']:info[hit['chunk_id']]['ranges']}
        for i in explanation+definitions+labels:
            pattern=_EXPLANATION if i in explanation else _DEFINITION
            ranges=[[m.start(),m.end()] for m in re.finditer(r'[^。；;\n]+(?:[。；;\n]|$)',pool[i]['text']) if pattern.search(m.group())]
            required[pool[i]['chunk_id']]=ranges or [[0,len(pool[i]['text'])]]
        key=(hit['document_id'],hit.get('parse_version_id'),tuple(ids))
        if key in seen:continue
        seen.add(key);linked.add(hit['chunk_id'])
        groups.append(dict(id='math-'+sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()[:20],
            document_id=hit['document_id'],parse_version_id=hit.get('parse_version_id'),chunk_ids=ids,
            formula_ids=[hit['chunk_id']],explanation_ids=[pool[i]['chunk_id'] for i in explanation],
            definition_ids=[pool[i]['chunk_id'] for i in definitions],
            required_ranges=required,
            link_basis='explicit_statement_reference',semantic_support_verified=False))
    return dict(version=MATH_EVIDENCE_VERSION,groups=groups,
        blocked_chunk_ids=[identifier for identifier,value in info.items() if value['state'] in {'empty_formula','unreadable_expression'}],
        formulas=info,unlinked_formula_count=sum(value['state']=='readable_expression' and identifier not in linked for identifier,value in info.items()),
        formula_semantics_verified=False)


def required_ranges(report):
    """强制保留关联组的真实关键句与公式，预算不够则整组舍弃。"""
    result={identifier:list(value['ranges']) for identifier,value in report.get('formulas',{}).items() if value['state']=='readable_expression'}
    for group in report.get('groups',[]):
        for identifier,ranges in group['required_ranges'].items():result.setdefault(identifier,[]).extend(ranges)
    return result


def admission_partners(ids, pairs=(), groups=()):
    """保留缺失成员，使阈值或候选上限删掉半组时不能默默装入另一半。"""
    components={identifier:{identifier} for identifier in ids}
    links=[[p['left_chunk_id'],p['right_chunk_id']] for p in pairs]+[g['chunk_ids'] for g in groups]
    for link in links:
        union=set(link)
        for identifier in link:union.update(components.get(identifier,()))
        for identifier in union:components[identifier]=union
    order={identifier:n for n,identifier in enumerate(ids)}
    return {identifier:sorted(group,key=lambda i:(order.get(i,len(order)),i)) for identifier,group in components.items()}


def group_hints(groups, available_ids, selected_ids):
    entries=[]
    for group in groups:
        ids=set(group['chunk_ids'])
        state='admitted' if ids<=selected_ids else ('candidate_unavailable' if not ids<=available_ids else 'not_admitted')
        entries.append(dict(id=group['id'],state=state,member_count=len(ids),
            selected_count=len(ids&selected_ids),formula_semantics_verified=False))
    return dict(version=MATH_EVIDENCE_VERSION,groups=entries,
        incomplete_count=sum(g['state']!='admitted' for g in entries),formula_semantics_verified=False)
