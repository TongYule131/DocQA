"""预标注适用范围的离线度量。标签和事实对齐来自人工，不自动推断语义。"""
from __future__ import annotations

import re
from app.rag_evaluation import digest


class ScopeEvaluationError(ValueError):
    pass


def _require(ok,code):
    if not ok:raise ScopeEvaluationError(code)


def _span(span,sources):
    _require(isinstance(span,dict) and set(span)=={'source_id','start','end','quote'},'invalid_source_span')
    _require(isinstance(span['source_id'],str),'invalid_source_span')
    source=sources.get(span['source_id'])
    start,end=span['start'],span['end']
    _require(source is not None and type(start) is int and type(end) is int
             and 0<=start<end<=len(source) and source[start:end]==span['quote'],'invalid_source_span')
    return span['quote']


def validate_annotation(annotation):
    _require(isinstance(annotation,dict) and set(annotation)=={
        'document_id','parse_version_id','sources','facts','scopes'},'invalid_annotation')
    _require(all(isinstance(annotation[k],str) and annotation[k] for k in ('document_id','parse_version_id')),
             'invalid_source_identity')
    _require(all(isinstance(annotation[k],list) for k in ('sources','facts','scopes')),'invalid_annotation')
    sources={}
    for source in annotation['sources']:
        _require(isinstance(source,dict) and set(source)=={'source_id','text','text_sha256'},'invalid_source')
        sid,text=source['source_id'],source['text']
        _require(isinstance(sid,str) and sid and sid not in sources and isinstance(text,str)
                 and digest(text)==source['text_sha256'],'source_hash_mismatch')
        sources[sid]=text
    facts={}
    for fact in annotation['facts']:
        _require(isinstance(fact,dict) and set(fact)=={'fact_id','anchor'},'invalid_fact_anchor')
        fid=fact['fact_id']
        _require(isinstance(fid,str) and fid and fid not in facts,'duplicate_fact_id')
        _span(fact['anchor'],sources);facts[fid]=fact
    seen=set()
    for scope in annotation['scopes']:
        _require(isinstance(scope,dict) and set(scope)=={'scope_id','kind','span','applies_to','excludes',
                                                       'basis','contradictions'},'invalid_scope')
        sid=scope['scope_id']
        _require(isinstance(sid,str) and sid and sid not in seen,'duplicate_scope_id');seen.add(sid)
        _span(scope['span'],sources)
        _require(isinstance(scope['kind'],str) and scope['kind'] in {'subject','period','unit','scope','condition','role'}
            and isinstance(scope['basis'],str) and scope['basis'] in {
                'explicit_text','row','column','section','exception','uncertain'},'invalid_scope_basis')
        for name in ('applies_to','excludes'):
            items=scope[name]
            _require(isinstance(items,list) and all(isinstance(i,str) and i in facts for i in items)
                     and len(set(items))==len(items),'unknown_scope_target')
        _require(scope['applies_to'] and not set(scope['applies_to']) & set(scope['excludes']),'conflicting_scope_targets')
        _require(isinstance(scope['contradictions'],list) and all(isinstance(v,str) and v for v in scope['contradictions']),
                 'invalid_contradiction_labels')
    _require(sources and facts and annotation['scopes'],'empty_annotation')
    return sources,facts


def bind_alignment(annotation,answer,assignments):
    """封存人工对齐与具体版本；该函数不从答案猜测source_fact_ids。"""
    return dict(annotation_sha256=digest(annotation),answer_sha256=digest(answer),facts=assignments)


def _conflicts(piece,phrase,opposites):
    # “含在途”本身可能是“不含在途”内的字串；不能把正确原句当反转。
    required=[m.span() for m in re.finditer(re.escape(phrase),piece)]
    return any(not any(left<=m.start() and m.end()<=right for left,right in required)
               for opposite in opposites for m in re.finditer(re.escape(opposite),piece))


def _quote_covers(citation,span,sources):
    if citation['chunk_id']!=span['source_id']:return False,False
    placements=[m.span() for m in re.finditer(re.escape(citation['quote']),sources[span['source_id']])]
    covers=[left<=span['start'] and span['end']<=right for left,right in placements]
    return bool(covers) and all(covers),any(covers) and not all(covers)


def evaluate_scoped_answer(annotation,answer,alignment):
    sources,source_facts=validate_annotation(annotation)
    if alignment is None:
        return dict(status='unreviewed',retention_gate_passed=False,checks=[],semantic_accuracy=None)
    _require(isinstance(alignment,dict) and set(alignment)=={'annotation_sha256','answer_sha256','facts'}
             and alignment['annotation_sha256']==digest(annotation) and alignment['answer_sha256']==digest(answer),
             'alignment_version_mismatch')
    _require(isinstance(answer,dict) and all(isinstance(answer.get(k,[]),list)
             for k in ('conclusion','explanation','citations')),'invalid_answer')
    facts=answer.get('conclusion',[])+answer.get('explanation',[])
    assignments=alignment['facts']
    _require(isinstance(assignments,list) and len(assignments)==len(facts),'incomplete_alignment')
    citations={}
    for citation in answer.get('citations',[]):
        _require(isinstance(citation,dict) and {'reference_id','chunk_id','quote','document_id','parse_version_id'}
                 <=set(citation),'citation_source_mismatch')
        ref=citation['reference_id'];sid=citation['chunk_id'];quote=citation['quote']
        _require(type(ref) is int and ref>0 and ref not in citations and isinstance(sid,str) and sid in sources
            and citation['document_id']==annotation['document_id']
            and citation['parse_version_id']==annotation['parse_version_id']
            and isinstance(quote,str) and quote and quote in sources[sid],'citation_source_mismatch')
        citations[ref]=citation
    checks=[]
    for i,(fact,assignment) in enumerate(zip(facts,assignments)):
        _require(isinstance(assignment,dict) and set(assignment)=={'answer_index','source_fact_ids'}
                 and type(assignment['answer_index']) is int and assignment['answer_index']==i,'incomplete_alignment')
        ids=assignment['source_fact_ids']
        _require(isinstance(ids,list) and ids and all(isinstance(fid,str) and fid in source_facts for fid in ids)
                 and len(set(ids))==len(ids),'unknown_aligned_fact')
        _require(isinstance(fact,dict) and isinstance(fact.get('text'),str) and isinstance(fact.get('refs'),list)
                 and fact['refs'] and all(type(r) is int and r in citations for r in fact['refs']),'invalid_answer_fact')
        _require(isinstance(fact.get('context',[]),list),'context_source_mismatch')
        pieces=[fact['text']]
        for entry in fact.get('context',[]):
            _require(isinstance(entry,dict) and set(entry)=={'kind','text','ref'} and type(entry['ref']) is int
                and isinstance(entry['kind'],str) and entry['kind'] in {'subject','period','unit','scope','condition','role'}
                and entry['ref'] in fact['refs'] and isinstance(entry['text'],str) and entry['text']
                and entry['text'] in citations[entry['ref']]['quote'],'context_source_mismatch')
            pieces.append(entry['text'])
        for scope in annotation['scopes']:
            applies=bool(set(ids)&set(scope['applies_to']));excluded=bool(set(ids)&set(scope['excludes']))
            if not (applies or excluded):continue
            phrase=scope['span']['quote']
            mentioned=any(phrase in piece for piece in pieces)
            # context绑定其自己的ref；不能借另一条事实或同块另一处相同短语补证。
            refs=(fact['refs'] if phrase in fact['text'] else [entry['ref'] for entry in fact.get('context',[])
                                                            if phrase in entry['text']])
            coverage=[_quote_covers(citations[r],scope['span'],sources) for r in refs]
            source_cited=any(c[0] for c in coverage)
            ambiguous_source=not source_cited and any(c[1] for c in coverage)
            if scope['basis']=='uncertain' or (applies and excluded):result='unverified_scope'
            elif applies and any(_conflicts(p,phrase,scope['contradictions']) for p in pieces):result='contradictory_phrase'
            elif applies and not mentioned:result='missing_qualification'
            elif applies and ambiguous_source:result='unverified_scope'
            elif applies and not source_cited:result='missing_qualification_source'
            elif applies:result='retained_literal'
            elif mentioned:result='out_of_scope_phrase'
            else:result='excluded_clean'
            checks.append(dict(answer_index=i,scope_id=scope['scope_id'],basis=scope['basis'],result=result))
    unknown=not facts or not checks or any(c['result']=='unverified_scope' for c in checks)
    failed=any(c['result'] not in {'unverified_scope','retained_literal','excluded_clean'} for c in checks)
    return dict(status='failed' if failed else 'unverified' if unknown else 'reviewed_literal_scope',
        retention_gate_passed=not failed and not unknown,checks=checks,
        annotation_sha256=digest(annotation),answer_sha256=digest(answer),
        semantic_accuracy=None,boundary='Requires human source-fact alignment and exact scope labels; literal retention is not semantic correctness')
