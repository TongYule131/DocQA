"""预算内的本地多路证据规划：关键词、向量、章节与邻接上下文。

只改变送入模型的原文集合，不调用模型改写问题，不生成派生事实，也不将
词面命中当作事实正确或答案覆盖率。来源和原文字串均来自固定解析版本。
"""
from __future__ import annotations

import math
import re
import json
from collections import Counter


def terms(text: str) -> list[str]:
    result = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*|\d+(?:\.\d+)?%?", text.lower())
    for sequence in re.findall(r"[\u4e00-\u9fff]+", text):
        result.extend(sequence[i:i + 2] for i in range(max(1, len(sequence) - 1)))
    return result


def question_parts(question: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[？?；;\n]+", question) if part.strip()]
    # 这是本地检索范围提示，并非模型判定的用户意图或完整性承诺。
    return parts[:8] or [question]


def _bm25(query: str, texts: list[str]) -> list[tuple[int, float]]:
    bags = [Counter(terms(text)) for text in texts]
    average = sum(sum(bag.values()) for bag in bags) / max(1, len(bags))
    vocabulary = set(terms(query))
    frequencies = {term: sum(term in bag for bag in bags) for term in vocabulary}
    scores = []
    for i, bag in enumerate(bags):
        length = sum(bag.values())
        score = 0.0
        for term in vocabulary:
            frequency = bag.get(term, 0)
            if not frequency:
                continue
            idf = math.log(1 + (len(bags) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / max(1, average)))
        if score > 0:
            scores.append((i, score))
    return sorted(scores, key=lambda item: (-item[1], item[0]))


def _plan_retrieval_v1(question: str, vector_result: dict, chunks: list, *, max_candidates: int = 40,
                   fulltext_budget: int | None = None, max_items: int = 20, payload_transform=None,
                   overview: bool = False) -> dict:
    version_id = vector_result.get("parse_version_id")
    document_ids = {hit.get("document_id") for hit in vector_result.get("results", [])}
    document_ids.update(chunk.document_id for chunk in chunks)
    if len(document_ids) != 1 or any(chunk.parse_version_id != version_id for chunk in chunks):
        raise ValueError("retrieval_scope_mismatch")
    # 原文、表头与注释不改写。未被向量top-k召回的词面命中仍保留来源。
    vector_hits = {hit["chunk_id"]: hit for hit in vector_result.get("results", [])}
    scores = vector_result.get("candidate_scores", {})
    # 全量分值仅在内存使用，不增加持久化证据或普通检索响应的规模。
    vector_result = {key: value for key, value in vector_result.items() if key != "candidate_scores"}
    pool = []
    for chunk in chunks:
        pool.append(dict(vector_hits.get(chunk.id) or {
            "chunk_id": chunk.id, "document_id": chunk.document_id,
            "parse_version_id": chunk.parse_version_id, "page": chunk.page, "text": chunk.text,
            "score": scores.get(chunk.id, 0.0), "chunk_type": chunk.chunk_type, "heading_path": chunk.heading_path,
            "order_index": chunk.order_index, "block_id": chunk.block_id,
            "sources": [source.model_dump() for source in chunk.sources],
        }))
    if not pool:
        return {**vector_result, "strategy": "hybrid-context-v1", "evidence_order": [], "results": []}
    # 只有完整原文及其真实元数据实际可装入预算，才标明完整阅读路由；绝不取前N块冒充全文。
    if fulltext_budget is not None and len(pool) <= max_items and (overview or any(
            marker in question for marker in ("全文", "概述", "整体", "主要内容", "总结"))):
        from app.rag_context import _candidate_from_hit
        entries = []
        for position, hit in enumerate(pool, 1):
            entry = _candidate_from_hit(hit)
            entry.reference_id = position
            entries.append(entry.payload())
        if len(json.dumps(payload_transform(entries) if payload_transform else entries,
                          ensure_ascii=False, separators=(",", ":"))) <= fulltext_budget:
            return {**vector_result, "strategy": "hybrid-context-v1", "results": pool,
                    "evidence_order": [hit["chunk_id"] for hit in pool],
                    "planning": {"route": "complete_indexed_text", "source_chunk_count": len(pool),
                                 "candidate_count": len(pool), "question_parts": question_parts(question),
                                 "semantic_completeness_verified": False}}
    texts = [(hit.get("heading_path") or "") + "\n" + hit["text"] for hit in pool]
    indexes = {hit["chunk_id"]: i for i, hit in enumerate(pool)}
    vector_ranks = [indexes[hit["chunk_id"]] for hit in vector_result.get("results", [])
                    if hit["chunk_id"] in indexes]
    parts = question_parts(question)
    rankings = [[i for i, _ in _bm25(part, texts)] for part in parts]
    rankings.append([i for i, _ in _bm25(question, texts)])
    rankings.append(vector_ranks)
    fusion = Counter()
    for ranking in rankings:
        for rank, index in enumerate(ranking):
            fusion[index] += 1 / (60 + rank + 1)
    fused = sorted(fusion, key=lambda i: (-fusion[i], i))
    # 每个实际问题部分先分配一个锚点，再按融合分数补充，避免一个部分垄断候选。
    anchors = []
    for ranking in rankings[:len(parts)]:
        if ranking and ranking[0] not in anchors:
            anchors.append(ranking[0])
    anchors += [i for i in fused if i not in anchors]
    selected = []
    reasons = {}

    def add(index: int, reason: str):
        if 0 <= index < len(pool) and index not in selected and len(selected) < max_candidates:
            selected.append(index)
            reasons[pool[index]["chunk_id"]] = reason

    # 对每个锚点保留上下文预算。邻接扩展尤其用于图题、跨页续句和单独标题。
    anchor_budget = max(1, min(len(anchors), max_candidates // 4))
    for anchor in anchors[:anchor_budget]:
        add(anchor, "question_part_or_rank_fusion")
    for anchor in anchors[:anchor_budget]:
        hit = pool[anchor]
        heading = hit.get("heading_path")
        # 同章节优先；没有章节元数据时仍可通过真实阅读顺序补相邻块。
        same_section = [i for i, other in enumerate(pool)
                        if heading and other.get("heading_path") == heading]
        if heading:
            path = heading.split(" / ")
            for depth, segment in enumerate(path):
                title = re.sub(r"^[\d一二三四五六七八九十百、.（）()\s]+", "", segment)
                if len(title) >= 3 and title in question:
                    prefix = " / ".join(path[:depth + 1])
                    same_section = [i for i, other in enumerate(pool)
                        if (other.get("heading_path") or "") == prefix or
                           (other.get("heading_path") or "").startswith(prefix + " / ")]
                    break
        for index in sorted(same_section, key=lambda i: (abs(i - anchor), i))[:5]:
            add(index, "same_section")
        for index in (anchor - 1, anchor + 1):
            add(index, "adjacent_context")
    for anchor in anchors:
        add(anchor, "rank_fusion")
    result = dict(vector_result)
    result.update(strategy="hybrid-context-v1", results=[pool[i] for i in selected],
                  evidence_order=[pool[i]["chunk_id"] for i in selected],
                  planning={"question_parts": parts, "source_chunk_count": len(pool),
                            "candidate_count": len(selected), "selection_reasons": reasons,
                            "semantic_completeness_verified": False})
    return result


RETRIEVAL_STRATEGY_VERSION = "hybrid-context-v10"


def _search_text(text: str) -> str:
    """只归一化检索视图中的汉字间OCR空格，绝不改原文或引用。"""
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)


# 规则只生成检索线索，不判定某项有答案，也不声称覆盖了全部用户意图。
_ASPECTS = (
    ("problems", r"困境|挑战|风险|不足", "困境 挑战 风险 不足 缺乏 薄弱 虚化",
     ("困境", "挑战", "不足", "缺乏", "薄弱", "虚化")),
    ("structure", r"层次|结构|组成|构成|要素", "层次 结构 组成 构成 原则 流程 要素",
     ("构成", "组成", "外环", "内环", "外层", "内层", "要素", "包括")),
    ("relationship", r"对应|关系|递进|发挥作用|如何回应", "对应 关系 回应 作用 载体 路径 策略 递进",
     ("对应", "回应", "作为", "载体", "路径", "策略", "阶梯", "进阶", "内化")),
    ("stages", r"课时|阶段|步骤|流程", "课时 阶段 步骤 流程 第一 第二 第三",
     ("课时", "阶段", "步骤", "流程", "第一", "第二", "第三")),
    ("activities", r"教什么|类比|情境|实践任务|主要任务|怎么做|如何实施", "教学 活动 类比 情境 实践 任务",
     ("类比", "情境", "活动", "任务", "学生", "训练", "测试")),
    ("outcomes", r"效果|成效|结果|验证|统计|关键数据", "效果 成效 结果 验证 统计 数据",
     ("效果", "统计", "正确率", "人均", "平均", "提高", "提升", "%")),
    ("conditions", r"条件|例外|口径|审计|范围|限制|区别|区分", "条件 例外 口径 范围 限制 单位 期间",
     ("条件", "例外", "未经审计", "不适用", "仅", "范围", "不代表", "空白", "缓存")),
)


def retrieval_aspects(question: str) -> list[dict]:
    result = [{"id": f"part-{i}", "query": part, "kind": "question_part", "markers": ()}
              for i, part in enumerate(question_parts(question), 1)]
    for kind, pattern, expansion, markers in _ASPECTS:
        if re.search(pattern, question):
            result.append({"id": kind, "query": expansion, "kind": kind, "markers": markers})
    return result


def _is_label(hit: dict) -> bool:
    text = _search_text(hit["text"]).strip()
    tail = _search_text(hit.get("heading_path") or "").split(" / ")[-1].strip()
    return bool(text == tail or (len(text) < 45 and not re.search(r"[。；;：:]", text)))


def _quoted_terms(text: str) -> list[str]:
    text = _search_text(text)
    return [part.strip() for match in re.findall(r"['\"‘“]([^'\"’”\n]{3,90})['\"’”]", text)
            for part in re.split(r"[、，,;；]", match) if 3 <= len(part.strip()) <= 28]


def _heading_views(pool: list[dict]) -> tuple[list[str], int]:
    """旧版本缺少父链时，只从真实编号标题提供搜索线索，不写回来源字段。"""
    result, hint, derived = [], "", 0
    for hit in pool:
        explicit=_search_text(hit.get("heading_path") or "")
        if explicit:
            hint=explicit
            result.append(explicit)
            continue
        text=_search_text(hit["text"]).strip()
        if (len(text)<=120 and "。" not in text and re.match(
                r"^(?:[（(]\s*[一二三四五六七八九十]+\s*[）)]|[一二三四五六七八九十]+\s*[、.]|[1-9]\s*[.．])",text)):
            hint=text
        result.append(hint)
        derived+=bool(hint)
    return result,derived


def _definition_and_section_anchors(question: str, pool: list[dict], texts: list[str],
                                    headings: list[str], labels: list[bool], scores: dict) -> list[tuple[int, str]]:
    """有限保护直接定义及用户明确指定章节的各小节正文，不读取答案或页码标签。"""
    selected = []
    named = _quoted_terms(question)
    definition = re.compile(r"意指|是指|指的是|定义为|定义是|被定义|means\b|defined as", re.I)
    if re.search(r"定义|含义|定位|概念|阐释|归入|一一对应|划分", question):
        matches = [i for i, text in enumerate(texts) if not labels[i] and definition.search(text)
                   and any(term in text for term in named)]
        for i in sorted(matches, key=lambda i: (-scores.get(i, 0), i))[:3]:
            selected.append((i, "named_direct_definition"))
    if re.search(r"观点|主张|失效|是否支持|是否赞成|是否反对|是否符合|不符合|因此", question):
        conclusions = [i for i, heading in enumerate(headings) if not labels[i]
                       and re.search(r"结论|结语|总结|conclusions?", heading, re.I)]
        if conclusions:
            selected.append((max(conclusions, key=lambda i: (scores.get(i, 0), -i)), "author_position_conclusion"))
    sections = set(re.findall(r"第\s*([1-9]\d*)\s*节", question))
    if not sections:
        return selected
    groups = {}
    for i, heading in enumerate(headings):
        if labels[i]:
            continue
        tail = heading.split(" / ")[-1]
        number = re.match(r"\s*([1-9]\d*)\.([1-9]\d*)(?:\D|$)", tail)
        if number and number[1] in sections:
            groups.setdefault(number[1] + "." + number[2], []).append(i)
    for _, group in list(groups.items())[:8]:
        # 定义词只是检索线索；代表段不等于该小节全部事实已覆盖。
        representative = max(group, key=lambda i: (scores.get(i, 0)
            + (max(scores.values(), default=1) * 0.3 if definition.search(texts[i]) else 0), -i))
        selected.append((representative, "explicit_section_body"))
    return selected


def _plan_retrieval_current(question: str, vector_result: dict, chunks: list, *, max_candidates: int = 40,
                   fulltext_budget: int | None = None, max_items: int = 20, payload_transform=None,
                   query_plan: dict | None = None) -> dict:
    """本地覆盖重排：先保留各子问和结构解释，再有限扩展并动态停止。

    保留真实余弦分值供阈值检查；融合和规则分数只决定顺序，不是置信度。
    不按文件名、答案标签或某一道题进行分支，也不发出额外远端请求。
    """
    if type(max_candidates) is not int or not 1 <= max_candidates <= 100:
        raise ValueError("invalid_candidate_limit")
    if type(max_items) is not int or not 1 <= max_items <= 100:
        raise ValueError("invalid_context_limit")
    from app.query_planning import plan_query, public_plan
    query_plan = query_plan or plan_query(question)
    ids = {c.document_id for c in chunks}
    ids.update(h.get("document_id") for h in vector_result.get("results",[]))
    version = vector_result.get("parse_version_id")
    if (len(ids) != 1 or any(c.parse_version_id != version for c in chunks) or
            any(h.get("parse_version_id") != version for h in vector_result.get("results",[]))):
        raise ValueError("retrieval_scope_mismatch")
    # 小文档才尝试完整阅读，不为大文档重复执行上一版的全量融合规划。
    if len(chunks) <= min(max_items, max_candidates):
        old = _plan_retrieval_v1(question,vector_result,chunks,max_candidates=max_candidates,
            fulltext_budget=fulltext_budget,max_items=max_items,payload_transform=payload_transform,
            overview=query_plan['task_type']=='document_overview')
        if not chunks or (old.get("planning") or {}).get("route") == "complete_indexed_text":
            old.setdefault('planning', {})['query_plan'] = public_plan(query_plan)
            if chunks:
                from app.rag_evidence_slots import evidence_slots, SLOT_PLAN_VERSION
                old['planning'].update(evidence_slots=evidence_slots(question,old['results'],query_plan=query_plan),
                    slot_plan_version=SLOT_PLAN_VERSION)
                if 'formula_check' in query_plan['facets']:
                    from app.rag_math_evidence import math_evidence
                    old['planning']['math_evidence']=math_evidence(old['results'],question)
            return {**old,"strategy":RETRIEVAL_STRATEGY_VERSION}
    hits = {h["chunk_id"]: h for h in vector_result.get("results", [])}
    cosines = vector_result.get("candidate_scores", {})
    pool = []
    for c in sorted(chunks, key=lambda c: (c.order_index if c.order_index is not None else 1 << 30, c.id)):
        pool.append(dict(hits.get(c.id) or dict(chunk_id=c.id, document_id=c.document_id,
            parse_version_id=c.parse_version_id, page=c.page, text=c.text,
            score=cosines.get(c.id, 0.0), chunk_type=c.chunk_type, heading_path=c.heading_path,
            order_index=c.order_index, block_id=c.block_id, sources=[s.model_dump() for s in c.sources])))
    texts = [_search_text(h["text"]) for h in pool]
    headings,derived_heading_count = _heading_views(pool)
    labels = [_is_label(h) for h in pool]
    aspects = retrieval_aspects(_search_text(question))
    global_lex = dict(_bm25(_search_text(question), texts))
    top_global = max(global_lex.values(), default=1.0)
    vector_rank = {h["chunk_id"]: i for i,h in enumerate(vector_result.get("results", []))}
    ranks, values = [], []
    for aspect in aspects:
        lexical = dict(_bm25(aspect["query"], texts))
        heading_scores = dict(_bm25(aspect["query"], headings))
        top = max(lexical.values(), default=1.0)
        top_heading = max(heading_scores.values(), default=1.0)
        scored = []
        for i, hit in enumerate(pool):
            score = lexical.get(i,0) / top + 0.22 * heading_scores.get(i,0) / top_heading
            score += 0.32 * global_lex.get(i,0) / top_global
            if hit["chunk_id"] in vector_rank:
                score += 0.12 / (1 + vector_rank[hit["chunk_id"]] / 8)
            # 正文中的定义和关系比仅在标题或背景复述问题更有用；仍只是线索。
            marker_count = sum(marker in texts[i] for marker in aspect["markers"])
            score += min(marker_count, 4) * 0.12
            if labels[i]:
                score *= 0.25
            if (re.search(r"背景|引言|综述|研究现状|background|introduction",headings[i],re.I)
                    and not re.search(r"背景|引言|综述|研究现状|background|introduction",question,re.I)):
                score *= 0.4
            if re.search(r"参考文献|references", headings[i], re.I) and not re.search(r"参考文献|文献目录|references",question,re.I):
                score *= 0.12
            scored.append(score)
        values.append(scored)
        ranks.append(sorted(range(len(pool)), key=lambda i: (-scored[i], i)))
    selected, reasons = [], {}
    def add(i, reason):
        if 0 <= i < len(pool) and i not in selected and len(selected) < max_candidates:
            selected.append(i)
            reasons[pool[i]["chunk_id"]] = reason

    # 课时/步骤的编号常嵌在PDF正文中，不能仅靠不完整的标题父链。
    stage_ranges = []
    if any(a["kind"] == "stages" for a in aspects):
        starts = [i for i,t in enumerate(texts) if re.search(
            r"(?:^|[。！？\n])\s*[1-9][.．]\s*[\u4e00-\u9fff]", t)]
        for n,start in enumerate(starts):
            end = starts[n+1] if n+1 < len(starts) else min(len(pool),start+12)
            # 不跨到已经明确变化的章节；同一课时至多读取12个相邻块。
            end = min(end,start+12)
            section = headings[start].split(" / ")[0]
            for i in range(start+1,end):
                if headings[i].split(" / ")[0] != section:
                    end=i
                    break
            if any(global_lex.get(i,0)>0 for i in range(start,end)):
                stage_ranges.append([i for i in range(start,end) if not labels[i]])
    # 有明确课时线索时先轮换保留每课时正文，避免前两课占满输入。
    for offset in range(6):
        for group in stage_ranges[:6]:
            if offset < len(group):
                add(group[offset],"numbered_stage_context")

    # 先装入可核对的定义和明确章节代表段，避免摘要/背景先占满字符预算。
    # 用户提供的数值仅是召回线索，绝不能作为事实证据；保护真实原文中的对应数据段。
    from app.rag_numeric_roles import source_number_tokens
    numeric_hints = source_number_tokens(question)
    numeric_anchors = []
    for number in sorted(numeric_hints):
        matches = [i for i,t in enumerate(texts) if not labels[i] and number in source_number_tokens(t)]
        if matches:
            best=max(matches,key=lambda i:(global_lex.get(i,0),-i))
            numeric_anchors.append(pool[best]['chunk_id']); add(best,'numeric_source_anchor')
    for i, reason in _definition_and_section_anchors(question, pool, texts, headings, labels, global_lex):
        add(i, reason)

    anchors = []
    for ranking in ranks:
        if ranking and ranking[0] not in anchors:
            anchors.append(ranking[0])
            add(ranking[0],"question_aspect_anchor")
    # 结构综述中实际出现的概念可链接解释小节，补足“要素叫什么”到“如何发挥作用”。
    linked = []
    for anchor in anchors:
        concepts = _quoted_terms(pool[anchor]["text"])
        for i, heading in enumerate(headings):
            if concepts and any(term in heading for term in concepts) and not labels[i]:
                if i not in linked:
                    linked.append(i)
    for i in linked[:max_items]:
        add(i,"named_element_explanation")

    for aspect,ranking in zip(aspects,ranks):
        if aspect["kind"] == "problems":
            root=headings[ranking[0]].split(" / ")[0] if ranking else ""
            scoped=[i for i in ranking if re.search(r"困境|挑战|风险|问题分析",root)
                    and headings[i].split(" / ")[0]==root]
            for i in (scoped or ranking)[:3]:
                add(i,"problem_context")
            if ranking:
                anchor=ranking[0]
                for i in (anchor-1,anchor+1):
                    if 0<=i<len(pool) and headings[i]==headings[anchor] and not labels[i]:
                        add(i,"problem_adjacent_context")

    # 同章节的决定性条件不能因主问只出现一个金额/期限而落在输入之外。
    for anchor in anchors:
        for i in (anchor-1,anchor+1):
            if 0 <= i < len(pool) and headings[i] == headings[anchor] and not labels[i]:
                if re.search(r"条件|例外|未经审计|不适用|注[：:]|仅|不代表|缓存|未填报",texts[i]):
                    add(i,"adjacent_condition")

    # 覆盖增益+词面冗余惩罚的确定性本地重排。动态停止不把低分当“无答案”。
    bags = [set(terms(t)) for t in texts]
    indexes = {hit["chunk_id"]:i for i,hit in enumerate(pool)}
    vector_floor = [indexes[h["chunk_id"]] for h in vector_result.get("results",[]) if h["chunk_id"] in indexes]
    initial = max((max(row) for row in values),default=1)
    floor = min(len(pool), max(4,len(question_parts(question))*2))
    while len(selected) < max_candidates:
        # 分布尚未校准时不凭本地词面分数删除真实向量候选，特别是抽象比较题。
        if len(set(selected)|set(vector_floor)) >= max_candidates:
            break
        best, best_score = None, -1.0
        for i in range(len(pool)):
            if i in selected or labels[i]:
                continue
            gains=[]
            for row in values:
                covered=max((row[j] for j in selected),default=0)
                gains.append(row[i] * (0.4 if covered >= row[i] else 1.0))
            relevance=max((row[i] for row in values),default=0)
            redundancy=max((len(bags[i]&bags[j])/max(1,len(bags[i]|bags[j])) for j in selected),default=0)
            score=max(gains,default=0)+0.25*relevance-0.35*redundancy
            if score > best_score:
                best,best_score=i,score
        if best is None:
            break
        relevance=max(row[best] for row in values)
        if len(selected) >= floor and relevance < initial*0.32:
            break
        add(best,"coverage_rerank")
    for i in vector_floor:
        add(i,"vector_candidate_floor")
    from app.rag_evidence_slots import evidence_slots, SLOT_PLAN_VERSION
    slots = evidence_slots(question, pool, query_plan=query_plan)
    from app.rag_math_evidence import math_evidence
    math_plan=math_evidence(pool,question) if 'formula_check' in query_plan['facets'] else {}
    # 为每个实际槽位留出候选空间，不能只保留先前宽泛排名中的候选。
    mandatory = list(dict.fromkeys(slot['candidate_ids'][0] for slot in slots if slot['candidate_ids']))
    preferred = list(dict.fromkeys(i for slot in slots for i in slot['candidate_ids'][:2]))
    definitions = [pool[i]['chunk_id'] for i in selected if reasons.get(pool[i]['chunk_id']) == 'named_direct_definition']
    section_support = [i for slot in slots if slot['kind'] in {'explicit_subsection','explicit_section'} for i in slot['candidate_ids']]
    page_support = [slot['candidate_ids'][0] for slot in slots if slot['kind']=='source_page' and slot['candidate_ids']]
    math_ids=[identifier for group in math_plan.get('groups',[]) for identifier in group['chunk_ids']]
    protected = list(dict.fromkeys(math_ids + page_support + definitions + numeric_anchors + mandatory + preferred + section_support))[:max_candidates]
    protected_indices = [indexes[i] for i in protected]
    selected = (protected_indices + [i for i in selected if i not in protected_indices])[:max_candidates]
    for identifier in protected:
        reasons.setdefault(identifier, 'evidence_slot_candidate')
    from app.rag_context import _candidate_from_hit
    from app.rag_extract_context import continuation_pairs
    pairs=continuation_pairs([_candidate_from_hit(hit) for hit in pool])
    selected_ids={pool[i]['chunk_id'] for i in selected}
    for pair in pairs:
        left,right=pair['left_chunk_id'],pair['right_chunk_id']
        if left in selected_ids or right in selected_ids:
            for identifier in (left,right):
                if identifier not in selected_ids and len(selected)<max_candidates:
                    selected.append(indexes[identifier]);selected_ids.add(identifier)
                    reasons[identifier]='sentence_continuation'
    # 实际候选上限不足时显式保留空槽，不能把被截掉的候选列为可用证据。
    actual_ids = {pool[i]['chunk_id'] for i in selected}
    slots = [{**slot, 'candidate_ids': [i for i in slot['candidate_ids'] if i in actual_ids]} for slot in slots]
    result={k:v for k,v in vector_result.items() if k != "candidate_scores"}
    result.update(strategy=RETRIEVAL_STRATEGY_VERSION,results=[pool[i] for i in selected],
        evidence_order=[pool[i]["chunk_id"] for i in selected],planning={
            "route":"coverage_ranked_evidence", "question_parts":question_parts(question),
            "aspects":[{"id":a["id"],"query":a["query"],"candidate_ids":[pool[i]["chunk_id"] for i in rank[:5]]}
                       for a,rank in zip(aspects,ranks)],
            "source_chunk_count":len(pool),"candidate_count":len(selected),
            "selection_reasons":reasons,"numbered_stage_groups":len(stage_ranges),
            "derived_search_heading_count":derived_heading_count,
            "dynamic_candidates":True,"semantic_completeness_verified":False})
    result['planning'].update(evidence_slots=slots, slot_plan_version=SLOT_PLAN_VERSION,
        numeric_slot_count=len(set(numeric_anchors)), query_plan=public_plan(query_plan))
    if math_plan:result['planning']['math_evidence']=math_plan
    return result


def input_coverage_hints(planning: dict, selected_ids: set[str]) -> dict:
    """只记录实际装入哪些检索线索，不能将它展示为事实覆盖或答案正确率。"""
    return {"heuristic_only":True,"semantic_completeness_verified":False,
        "aspects":[{"id":a["id"],"selected_chunk_ids":[i for i in a["candidate_ids"] if i in selected_ids]}
                   for a in planning.get("aspects",[])]}


def plan_retrieval(question, vector_result, chunks, *, reranker=None, **kwargs):
    from app.retrieval_channels import attach_channels
    result = _plan_retrieval_current(question, vector_result, chunks, **kwargs)
    return attach_channels(question, vector_result, chunks, result, reranker=reranker) if chunks else result
