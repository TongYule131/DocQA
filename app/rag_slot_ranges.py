"""槽位线索的原文偏移；搜索视图去OCR空格后仍能回到未改写的源文。"""
import re

SLOT_RANGE_VERSION = 'source-slot-ranges-v1'


def matching_ranges(text,pattern):
    omitted=set()
    for match in re.finditer(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])',text):
        omitted.update(range(match.start(),match.end()))
    offsets=[n for n in range(len(text)) if n not in omitted]
    view=''.join(text[n] for n in offsets)
    return [[offsets[m.start()],offsets[m.end()-1]+1] for m in re.finditer(pattern,view,re.I) if m.start()<m.end()]


def visible_ranges(items):
    from app.rag_extract_context import admitted_ranges
    return {i.chunk_id:admitted_ranges(i.text,i.read_ranges,strict=i.segment_policy=='sentence-segments-v2') for i in items}


def contains_anchor(ranges,anchors):
    return any(start<=a and b<=end for a,b in anchors for start,end in ranges)


def required_slot_ranges(slots,items):
    """为定义、案例、数值等定位锚点保留最短完整句，不强塞整节或所有关键词命中。"""
    from app.rag_extract_context import sentence_ranges
    by_id={i.chunk_id:i for i in items};result={}
    for slot in slots:
        if slot.get('range_kind')!='anchor' or slot['kind']=='question_part':continue
        for identifier in slot['candidate_ids']:
            if identifier not in by_id:continue
            text=by_id[identifier].text;sentences=sentence_ranges(text)
            choices=[(n,a,b) for n,(a,b) in enumerate(sentences) if contains_anchor([[a,b]],slot['candidate_ranges'].get(identifier,[]))]
            if not choices:continue
            n,a,b=min(choices,key=lambda row:(row[2]-row[1],row[1]))
            result.setdefault(identifier,[]).append([a,b])
            for neighbor in (n-1,n+1):
                if 0<=neighbor<len(sentences):
                    x,y=sentences[neighbor]
                    if re.search(r'但|然而|例外|仅|前提|条件|不适用|未经审计|未审计|单位|期间',text[x:y]):
                        result[identifier].append([x,y])
    return result
