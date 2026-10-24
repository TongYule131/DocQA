"""确定性原文抽取：保留完整来源文本，只限制模型能看到/引用的连续范围。"""
from __future__ import annotations

import re

SELECTION_VERSION = 'extractive-passages-v7'
_TERMINAL = re.compile(r'[。！？!?;；.][\s”’\"\'）)\]】]*$')


def sentence_ranges(text,*,split_lines=False):
    text = text.replace('\r\n', '\n')
    result=[];start=0
    pattern=r'(?:[。！？!?；;]|…{2,}(?=\s*\[\d))[”’\"\'）)\]】]*\s*'
    if split_lines:pattern+=r'|\n+'
    for match in re.finditer(pattern,text):
        if text[start:match.end()].strip():result.append((start,match.end()))
        start=match.end()
    if text[start:].strip():result.append((start,len(text)))
    return result


def support_sentence_ranges(text,*,split_lines=False):
    """句子正文不强制包含外围空白/引号或独立文献标号；正文字符一律不清洗。"""
    result=[]
    for start,end in sentence_ranges(text,split_lines=split_lines):
        leading=re.match(r"(?:\s|[\"'“‘”’]|\[\d+(?:[-,]\d+)*\])+",text[start:end])
        if leading:start+=leading.end()
        trailing=re.search(r"[\s\"'”’]+$",text[start:end])
        if trailing:end=start+trailing.start()
        if start<end:result.append((start,end))
    return result


def strict_segments(text):
    """优先在第一个完整句末切分，避免一个小段跨过句末又停在下一句中间。"""
    result, start = [], 0
    while start < len(text):
        end = min(len(text), start + 60)
        stop = re.search(r'(?:[。！？!?；;\n]|…{2,}(?=\s*\[\d))[”’\"\'）)\]】]*', text[start:end])
        if stop:
            end = start + stop.end()
        elif end < len(text):
            comma = max(text.rfind(mark, start + 20, end) for mark in '，,：:|')
            if comma >= start + 20: end = comma + 1
        # 文献标号仍独立，不清洗原字符。
        if text[start] == '[' and 0 < text.find(']', start + 1, start + 16) - start < 16:
            end = text.index(']', start + 1) + 1
        else:
            bracket = text.find('[', start, end)
            if bracket > start: end = bracket
        result.append(text[start:end]); start = end
    return result


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges,key=lambda value:(value[0],value[1])):
        if merged and start <= merged[-1][1]: merged[-1][1] = max(end, merged[-1][1])
        else: merged.append([start, end])
    return merged


def validate_ranges(text, ranges):
    if (not isinstance(ranges, list) or not ranges or any(not isinstance(r, list) or len(r) != 2
            or any(type(v) is not int for v in r) or not 0 <= r[0] < r[1] <= len(text) for r in ranges)
            or merge_ranges(ranges) != ranges):
        raise ValueError('invalid_admitted_ranges')


def budget_profile(question, *, max_items, context_chars):
    from app.query_planning import plan_query, TASKS
    plan = plan_query(question)
    kind = plan['task_type']
    count = max(TASKS[f][1] for f in plan['facets'])
    excerpt = min(TASKS[f][2] for f in plan['facets'])
    return {'kind': kind, 'max_items': max_items, 'target_items': min(max_items, count), 'context_chars': context_chars,
            'excerpt_chars': excerpt, 'semantic_completeness_verified': False}


def extract_ranges(text, question, chunk_type, *, max_chars=700):
    """仅选原文句子；表格/短文本保留全文，不截单位、条件或长句凑长度。"""
    text = text.replace('\r\n', '\n')
    if not text: return []
    if chunk_type not in {None, 'text', 'paragraph', 'long_line'} or len(text) <= max_chars:
        return [[0, len(text)]]
    from app.retrieval_planning import _bm25,_search_text
    spans = sentence_ranges(text)
    scores = dict(_bm25(_search_text(question), [_search_text(text[a:b]) for a,b in spans]))
    if not scores: return [[0, len(text)]]  # 无匹配不假装找到充分证据。
    chosen = []
    for i in sorted(scores, key=lambda i: (-scores[i], i)):
        a,b = spans[i]
        if chosen and sum(y-x for x,y in merge_ranges(chosen + [[a,b]])) > max_chars: continue
        chosen.append([a,b])
        # 保留同一段内邻近的主体/条件/例外；不把单句抽取当成充分支持证明。
        for j in (i-1,i+1):
            if not 0 <= j < len(spans): continue
            x,y = spans[j]
            if re.search(r'但|然而|除外|例外|仅|前提|条件|不适用|未经审计|未审计|单位|期间', text[x:y]):
                chosen.append([x,y])
        if sum(y-x for x,y in merge_ranges(chosen)) >= max_chars: break
    # 指代/总结句依赖前句，后续代词句也常补完同一案例的困难或结论。
    # 此闭包可超过软字符目标；绝不为凑目标删除被保留的必要衔接。
    dependent=re.compile(r"^[\s'\"“‘]*(?:其中|因此|由此|这(?:些|种|一|个|就|也|是)|上述|该|其(?:中|实|他|所|研发)?|他们|它们|很多|原因之|然而|但|除外|例外|仅|前提|条件)")
    indices={i for i,(a,b) in enumerate(spans) if any(x<b and y>a for x,y in chosen)}
    changed=True
    while changed:
        before=set(indices)
        for i in before:
            if i>0 and dependent.search(text[slice(*spans[i])]):indices.add(i-1)
            if i+1<len(spans) and dependent.search(text[slice(*spans[i+1])]):indices.add(i+1)
        changed=before!=indices
    return merge_ranges([spans[i] for i in indices]) or [[0,len(text)]]


def continuation_pairs(items):
    """只在版本/章节一致、阅读顺序严格相邻的正文间链接续句，不合成原文。"""
    ordered = sorted(items, key=lambda i: (i.order_index if type(i.order_index) is int else 1 << 30, i.chunk_id))
    result=[]
    for left,right in zip(ordered,ordered[1:]):
        if (type(left.order_index) is not int or right.order_index != left.order_index+1
                or left.document_id != right.document_id or left.parse_version_id != right.parse_version_id
                or left.heading_path != right.heading_path
                or left.chunk_type not in {None,'text','paragraph','long_line'}
                or right.chunk_type not in {None,'text','paragraph','long_line'}): continue
        ending=re.sub(r'(?:\[\d+(?:[-,]\d+)*\]\s*)+$','',left.text)
        if len(left.text.strip()) < 45 or not right.text.strip() or _TERMINAL.search(ending): continue
        # 编号标题、列表的新条目不当作前文续句。
        if re.match(r'^\s*(?:第.{1,8}[节章]|[1-9]\d*[.．、]|[（(][一二三四五六七八九十]+[）)])', right.text): continue
        left_body=support_sentence_ranges(left.text);right_body=support_sentence_ranges(right.text)
        if not left_body or not right_body:continue
        result.append({'left_chunk_id':left.chunk_id,'left_range':list(left_body[-1]),
                       'right_chunk_id':right.chunk_id,'right_range':list(right_body[0])})
    return result


def visible_segments(text, ranges=None, *, strict=False):
    """窗口编号与原文位置保持稳定；抽取时只输出所需小段，重复窗口尽量省略。"""
    from app.quote_spans import quote_choice_ranges, context_segments
    text=text.replace('\r\n','\n')
    if ranges is not None: validate_ranges(text,ranges)
    windows=[]
    splitter=strict_segments if strict else context_segments
    for number,(a,b) in enumerate(quote_choice_ranges(text),1):
        pos=a; segments={}; positions={}
        for index, value in enumerate(splitter(text[a:b]),1):
            end=pos+len(value)
            if ranges is None or any(pos<y and end>x for x,y in ranges):
                segments[str(index)]=value;positions[index]=(pos,end)
            pos=end
        if segments: windows.append((number,segments,positions))
    if ranges is None: return windows
    selected=[];covered=[]
    for window in sorted(windows,key=lambda w:(-sum(b-a for a,b in w[2].values()),w[0])):
        segments={};positions={}
        for index,(a,b) in window[2].items():
            if not any(x<=a and b<=y for x,y in covered):
                segments[str(index)]=window[1][str(index)];positions[index]=(a,b)
        if positions:
            selected.append((window[0],segments,positions));covered=merge_ranges(covered+list(positions.values()))
    return sorted(selected,key=lambda w:w[0])


def admitted_ranges(text, ranges, *, strict=False):
    return merge_ranges([position for _,_,positions in visible_segments(text,ranges,strict=strict) for position in positions.values()])


def quote_units(text,ranges,*,strict=True):
    """预先给出完整句的窗口/小段组合，模型选择前可见，不在生成后补引文。"""
    windows=visible_segments(text,ranges,strict=strict)
    actual=merge_ranges([p for _,_,positions in windows for p in positions.values()])
    result={}
    for number,(a,b) in enumerate(support_sentence_ranges(text,split_lines=not strict),1):
        if not any(x<=a and b<=y for x,y in actual):continue
        parts=[];covered=[]
        for span,_,positions in windows:
            chosen=[i for i,(x,y) in positions.items() if x<b and y>a and not any(s<=x and y<=e for s,e in covered)]
            if not chosen:continue
            groups=[]
            for i in chosen:
                if groups and i==groups[-1][-1]+1:groups[-1].append(i)
                else:groups.append([i])
            for group in groups:
                parts.append([span,group[0],group[-1]])
                covered=merge_ranges(covered+[positions[i] for i in group])
            if any(x<=a and b<=y for x,y in covered):break
        if any(x<=a and b<=y for x,y in covered):result[str(number)]=parts
    return result


def assign_unit_options(items,pairs):
    """跨页原句在生成前成为同一选项；仍展开为各原块的独立引用范围。"""
    from app.quote_spans import quote_choice_ranges,context_segments
    by_chunk={item.chunk_id:item for item in items};base={};positions={}
    for item in items:
        ref=item.reference_id
        units=passage_units(item.text,item.read_ranges) if item.segment_policy=='sentence-segments-v2' else quote_units(item.text,item.read_ranges,strict=False)
        windows=quote_choice_ranges(item.text)
        splitter=strict_segments if item.segment_policy=='sentence-segments-v2' else context_segments
        for unit,parts in units.items():
            node=(ref,unit);base[node]=[[ref,*part] for part in parts];bounds=[]
            for span,first,last in parts:
                a,b=windows[span-1];segments=splitter(item.text[a:b]);start=a+sum(map(len,segments[:first-1]))
                end=a+sum(map(len,segments[:last]));bounds.append([start,end])
            positions[node]=merge_ranges(bounds)
    parent={node:node for node in base};removed=set()
    def find(node):
        while parent[node]!=node:
            parent[node]=parent[parent[node]];node=parent[node]
        return node
    for pair in pairs:
        left=by_chunk.get(pair['left_chunk_id']);right=by_chunk.get(pair['right_chunk_id'])
        sides=[]
        for item,key in ((left,'left_range'),(right,'right_range')):
            a,b=pair[key]
            sides.append([node for node,ranges in positions.items() if item is not None and node[0]==item.reference_id
                          and any(x<=a and b<=y for x,y in ranges)])
        if not all(sides):
            removed.update(node for side in sides for node in side);continue
        first=sides[0][0]
        for node in sides[0]+sides[1]:parent[find(node)]=find(first)
    components={}
    for node,parts in base.items():components.setdefault(find(node),[]).extend(parts)
    invalid={find(node) for node in removed}
    for item in items:
        item.unit_choices={unit:sorted({tuple(p) for p in components[find((ref,unit))]}) for ref,unit in base
                           if ref==item.reference_id and find((ref,unit)) not in invalid}
        item.unit_choices={unit:[list(p) for p in parts] for unit,parts in item.unit_choices.items()}


def passage_units(text,ranges):
    """正文每个连续抽取段为一个选项，保留同段指代与解释，不跨未读缺口。

    句级选择容易只引“这样的评价过程”而丢掉被指代的定义。这里在生成前
    明示整个实际可见段的范围；仍是逐字原文，既不扩入隐藏句也不改模型输出。
    表格继续使用独立行/句单元，避免将无关数值和脚注混成一个规则。
    """
    windows=visible_segments(text,ranges,strict=True)
    actual=merge_ranges([p for _,_,positions in windows for p in positions.values()])
    result={}
    for unit,(a,b) in enumerate(actual,1):
        parts=[]
        for span,_,positions in windows:
            chosen=[i for i,(x,y) in positions.items() if a<=x and y<=b]
            groups=[]
            for i in chosen:
                if groups and i==groups[-1][-1]+1:groups[-1].append(i)
                else:groups.append([i])
            parts.extend([span,group[0],group[-1]] for group in groups)
        if parts:result[str(unit)]=parts
    return result


def plan_admission(items, question, profile, *, required_ranges=None):
    for item in items:
        item.read_ranges=([[0,len(item.text)]] if profile['kind']=='document_overview' else
            extract_ranges(item.text,question,item.chunk_type,max_chars=profile['excerpt_chars']))
        if required_ranges and item.chunk_id in required_ranges:
            for start,end in required_ranges[item.chunk_id]:
                if not 0<=start<end<=len(item.text):raise ValueError('required_range_out_of_source')
            item.read_ranges=merge_ranges(item.read_ranges+required_ranges[item.chunk_id])
        item.segment_policy='sentence-segments-v2' if item.chunk_type in {None,'text','paragraph','long_line'} else None
    possible=continuation_pairs(items)
    by_id={item.chunk_id:item for item in items}
    active=[]
    # 续句关系按实际选中的首/尾范围传播，直至固定点；不拼接正文。
    changed=True
    while changed:
        changed=False
        for pair in possible:
            left,right=by_id[pair['left_chunk_id']],by_id[pair['right_chunk_id']]
            a,b=pair['left_range'];x,y=pair['right_range']
            needed=any(s<b and e>a for s,e in left.read_ranges) or any(s<y and e>x for s,e in right.read_ranges)
            if not needed:continue
            if pair not in active:active.append(pair)
            for item,bounds in ((left,[a,b]),(right,[x,y])):
                expanded=merge_ranges(item.read_ranges+[bounds])
                if expanded!=item.read_ranges:item.read_ranges=expanded;changed=True
    groups={i:{i} for i in by_id}
    for pair in active:
        a,b=pair['left_chunk_id'],pair['right_chunk_id']
        union=groups[a]|groups[b]
        for identifier in union:groups[identifier]=union
    positions={item.chunk_id:n for n,item in enumerate(items)}
    return active,{identifier:sorted(group,key=positions.get) for identifier,group in groups.items()}
