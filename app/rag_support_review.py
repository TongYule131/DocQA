"""逐论断证据检查：确定性范围/续句/标签约束与语义风险分开，绝不宣称一般蕴含。"""
from __future__ import annotations

import re

from app.rag_extract_context import SELECTION_VERSION, admitted_ranges, merge_ranges, support_sentence_ranges
from app.rag_validation import ModelOutputError

REVIEW_VERSION='claim-support-rules-v5'
_STRUCTURE=re.compile(r'不能(?:由|因为|仅凭)|数量相同.{0,18}(?:推出|说明|证明|不构成|不足以)|(?:划分依据|分类轴)(?:并)?不同|局部(?:交叠|交叉)|不能(?:直接|简单|机械)?(?:等同|对应|归入)|不是.{0,30}(?:直接同义|同义项)|不(?:是|能)(?:简单|机械)?一一对应|可(?:以)?推断|根据.{0,12}(?:结构|分类).{0,10}判断')


def _contains(ranges,start,end):
    return any(a<=start and end<=b for a,b in ranges)


def review_claims(output,pack,*,enforce=True):
    if pack.review_policy!=SELECTION_VERSION:return
    by_ref={item.reference_id:item for item in pack.items}
    by_chunk={item.chunk_id:item for item in pack.items}
    reports=[]
    for fact_index,fact in enumerate(output.conclusion+output.explanation):
        errors=[]
        supports={};quotes=[]
        for ref in fact.refs:
            binding=output.citation_bindings.get(ref)
            if not binding:raise ModelOutputError('claim_evidence_invalid')
            item=by_ref.get(binding['source_ref'])
            if item is None:raise ModelOutputError('quote_not_in_admitted_input')
            a,b=binding['char_start'],binding['char_end']
            actual=admitted_ranges(item.text,item.read_ranges,strict=item.segment_policy=='sentence-segments-v2')
            if not _contains(actual,a,b):raise ModelOutputError('quote_not_in_admitted_input')
            supports.setdefault(item.chunk_id,[]).append([a,b]);quotes.append(output.quotes[ref])
        supports={key:merge_ranges(ranges) for key,ranges in supports.items()}
        risks=[]
        for chunk_id,ranges in supports.items():
            item=by_chunk[chunk_id]
            if item.segment_policy!='sentence-segments-v2':continue
            for start,end in support_sentence_ranges(item.text):
                if start==end:continue
                if any(a<end and b>start for a,b in ranges) and not _contains(ranges,start,end):
                    errors.append('incomplete_quote_sentence')
        for pair in pack.continuation_pairs:
            left,right=pair['left_chunk_id'],pair['right_chunk_id']
            a,b=pair['left_range'];x,y=pair['right_range']
            left_hit=any(s<b and e>a for s,e in supports.get(left,[]))
            right_hit=any(s<y and e>x for s,e in supports.get(right,[]))
            if (left_hit or right_hit) and not (_contains(supports.get(left,[]),a,b)
                    and _contains(supports.get(right,[]),x,y)):
                errors.append('continuation_not_bound_to_claim')
        joined='\n'.join(quotes)
        match=_STRUCTURE.search(fact.text)
        if match:
            # 原文明示同样关系时允许直述；缺少该明确表述的结构判断必须标推断。
            explicit=re.sub(r'\s+','',match.group()) in re.sub(r'\s+','',joined)
            if not explicit and fact.claim_type!='inference':errors.append('inference_label_required')
            if not explicit:risks.append('structural_inference_requires_review')
        if re.search(r'(?:原文|论文)(?:并)?(?:没有|未)(?:说|说明|宣告|出现|逐项)',fact.text):
            risks.append('absence_scope_requires_review')
        # 只能提示缺少字面锚点，不能把同义改写当作语义错误或据命中率判蕴含。
        anchors=re.findall(r'《([^》]{2,60})》|[“\"]([^”\"]{2,40})[”\"]',fact.text)
        normalized=re.sub(r'\s+','',joined).lower()
        if any(re.sub(r'\s+','',a or b).lower() not in normalized for a,b in anchors):
            risks.append('claim_anchor_not_in_own_quotes')
        from app.rag_phrase_match import is_question_framing_term
        if any(is_question_framing_term(a or b,fact) and re.sub(r'\s+','',a or b).lower() not in normalized for a,b in anchors):
            risks.append('analysis_term_not_verbatim_source')
        # 明确的否定方向差异只是风险，不用通用词面规则代替复杂语义判断。
        for denied in re.findall(r'不应([^。；\n]{2,18})',joined):
            core=re.split(r'[，,]',denied)[0]
            if '应'+core in fact.text and '不应'+core not in fact.text:risks.append('possible_polarity_conflict')
        fact.support_review={'version':REVIEW_VERSION,'status':'needs_review' if risks else 'checks_passed',
            'semantic_status':'not_reviewed','checks':['admitted_input','complete_sentence','claim_local_continuation','inference_label'],
            'risk_codes':list(dict.fromkeys(risks))}
        reports.append({'fact_index':fact_index,'errors':list(dict.fromkeys(errors)),'review':fact.support_review})
        if errors and enforce:raise ModelOutputError(errors[0])
    return reports


def restore_admission(repository,snapshot,selected,retrieval,question):
    """发布时从不可变块重新计算抽取，防止检查点扩大模型可引用范围。"""
    from app.rag_context import _candidate_from_hit, EvidencePack
    from app.rag_extract_context import budget_profile,plan_admission
    if snapshot.get('evidence_selection_version')!=SELECTION_VERSION:raise ValueError('unknown_selection_version')
    profile=snapshot.get('evidence_profile') or {}
    expected=budget_profile(question,max_items=profile.get('max_items',0),context_chars=profile.get('context_chars',0))
    if expected!=profile:raise ValueError('admission_profile_changed')
    # 快照按当前问题固定预算；生成时使用含会话上下文的检索问题做抽取，恢复必须重放同一输入。
    from app.conversation_memory import MEMORY_SIGNATURE,retrieval_question,valid_memory
    memory=snapshot.get('conversation_context')
    if 'conversation_context' in snapshot and snapshot.get('conversation_policy_signature')!=MEMORY_SIGNATURE:
        raise ValueError('conversation_policy_changed')
    if memory is not None and not valid_memory(memory):
        raise ValueError('conversation_context_invalid')
    question=retrieval_question(question,memory)
    profile=budget_profile(question,max_items=profile['max_items'],context_chars=profile['context_chars'])
    chunks={c.id:c for c in repository.chunks(snapshot['document_id'],snapshot['parse_version_id'])}
    candidates=[]
    for hit in retrieval.get('results',[]):
        chunk=chunks.get(hit.get('chunk_id'))
        if chunk is None:raise ValueError('admission_source_changed')
        candidates.append(_candidate_from_hit(dict(chunk.model_dump(),chunk_id=chunk.id,score=hit.get('score',0))))
    pairs,_=plan_admission(candidates,question,profile)
    expected_items={item.chunk_id:item for item in candidates}
    items=[]
    for raw in selected:
        item=expected_items.get(raw.get('chunk_id'))
        if (item is None or item.read_ranges!=raw.get('read_ranges') or item.segment_policy!=raw.get('segment_policy')):
            raise ValueError('admission_checkpoint_changed')
        item.reference_id=raw['reference_id'];items.append(item)
    from app.rag_extract_context import assign_unit_options
    assign_unit_options(items,pairs)
    if any(item.unit_choices!=raw.get('unit_choices') for item,raw in zip(items,selected)):
        raise ValueError('unit_choices_checkpoint_changed')
    return EvidencePack(items=items,candidates=candidates,index_id=snapshot['index_id'],
        parse_version_id=snapshot['parse_version_id'],truncated=False,context_chars=0,
        review_policy=SELECTION_VERSION,continuation_pairs=pairs,budget_profile=profile)
