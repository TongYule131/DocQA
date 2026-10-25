"""知识库检索漏斗：分开召回规模、候选规模和最终上下文预算。"""
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256
import json

from app.rag_context import EvidencePack, build_evidence, pack_evidence
from app.rag_extract_context import assign_unit_options
from app.quote_spans import task_segment_payload
from app.retrieval_planning import plan_retrieval
from app.retrieval_channels import configured_reranker, admission_report
from app.rag_evidence_slots import slot_hints
from app.query_planning import plan_query, public_plan
from app.rag_source_diagnostics import source_diagnostics
from app.rag_math_evidence import admission_partners,group_hints
from app.rag_slot_ranges import visible_ranges

PIPELINE_VERSION = 'grounded-retrieval-pipeline-v7'


@dataclass(frozen=True)
class RetrievalBudget:
    recall_limit: int
    candidate_limit: int
    context_limit: int
    context_chars: int

    def __post_init__(self):
        if (any(type(v) is not int for v in self.__dict__.values()) or
                not 1 <= self.recall_limit <= 20 or not 1 <= self.candidate_limit <= 100
                or not 1 <= self.context_limit <= 100 or self.context_chars < 1000):
            raise ValueError('invalid_retrieval_budget')


class PipelineError(ValueError):
    pass


def knowledge_context(index, repository, documents, question, search_question, settings, budget,
                      *, query_plan=None, check=lambda: None, observe=lambda *args: nullcontext(), reranker_expected_hash=None, reranker_adapter=None, planning_question=None):
    """所有通道只读同一冻结版本；观察器只记时，不记录用户正文。"""
    query_plan = query_plan or plan_query(question)
    planning_question=planning_question or question
    reranker = reranker_adapter or configured_reranker(settings,reranker_expected_hash)
    packs, diagnostics, slots_by_key = [], [], {}
    for doc in documents:
        check()
        with observe('向量召回', 'RETRIEVE_VECTOR'):
            found = index.search_snapshot(doc['document_id'], search_question, budget.recall_limit,
                snapshot=doc['snapshot'], include_scores=True)
        chunks = repository.chunks(doc['document_id'], doc['snapshot']['parse_version_id'])
        blocks=repository.blocks(doc['snapshot']['parse_version_id'])
        with observe('解析质量与数值角色诊断', 'SOURCE_QUALITY'):
            version = repository.get_parse_version(doc['snapshot']['parse_version_id'])
            quality = source_diagnostics(chunks, version,blocks=blocks)
            if quality['quality_status'] == 'invalid': raise PipelineError('source_quality_invalid')
        with observe('关键词、结构线索与融合重排', 'RETRIEVE_FUSION'):
            planned = plan_retrieval(planning_question, found, chunks, max_candidates=budget.candidate_limit,
                fulltext_budget=budget.context_chars, max_items=budget.context_limit,
                payload_transform=task_segment_payload, query_plan=query_plan, reranker=reranker)
            for slot in planned.get('planning', {}).get('evidence_slots', []):
                key = ('global',slot['key']) if slot.get('scope') == 'global' else (doc['document_id'],slot['id'])
                combined = slots_by_key.setdefault(key, dict(slot, id=f'global-slot-{len(slots_by_key)+1}',candidate_ids=[],candidate_ranges={}))
                combined['candidate_ids'] += [i for i in slot['candidate_ids'] if i not in combined['candidate_ids']]
                combined['candidate_ranges'].update(slot.get('candidate_ranges',{}))
        with observe('原文范围核对与抽取压缩', 'EVIDENCE'):
            built = build_evidence(planned, settings, expected_document_id=doc['document_id'],
                max_items=budget.context_limit, context_max_chars=budget.context_chars,
                payload_transform=task_segment_payload,
                table_blocks=blocks, extractive_question=planning_question)
            if built.fatal_reason: raise PipelineError('source_scope_invalid')
        packs.append(built.pack)
        diagnostics.append(dict(document_id=doc['document_id'],parse_version_id=doc['snapshot']['parse_version_id'],
            source_count=len(chunks),vector_count=len(found.get('results',[])),
            candidate_count=len(planned.get('results',[])),admitted_count=len(built.pack.items),
            channels=planned.get('planning',{}).get('channel_contract',{}).get('channel_hits',{}),
            fusion=admission_report(planned.get('planning',{}).get('channel_contract',{}),[i.chunk_id for i in built.pack.items],built.pack.drop_reasons),
            _discard_details=built.pack.drop_reasons,
            numeric_slots=planned.get('planning',{}).get('numeric_slot_count',0),
            retrieval_route=planned.get('planning',{}).get('route'), source_diagnostics=quality,
            math_evidence=built.pack.math_hints))
    with observe('跨资料去重与上下文预算装入', 'CONTEXT_PACK'):
        packs.sort(key=lambda p:max((i.score for i in p.candidates),default=0),reverse=True)
        candidates, seen = [], set()
        # 使用已经核对、抽取过的候选，不先做局部装入再丢失另一资料的关键槽位。
        for n in range(max((len(p.candidates) for p in packs),default=0)):
            for pack in packs:
                if n >= len(pack.candidates): continue
                item = pack.candidates[n]; key=(item.document_id,item.parse_version_id,item.chunk_id)
                if key not in seen: candidates.append(item); seen.add(key)
        pairs=[pair for pack in packs for pair in pack.continuation_pairs]
        math_groups=[g for pack in packs for g in pack.admission_groups]
        partners=admission_partners([i.chunk_id for i in candidates],pairs,math_groups)
        order={i.chunk_id:n for n,i in enumerate(candidates)}
        slots = list(slots_by_key.values())
        for slot in slots:
            slot['candidate_ids'] = sorted((i for i in slot['candidate_ids'] if i in order), key=order.get)
        names = {d['document_id']:d['filename'] for d in documents}
        def serialize(items):
            assign_unit_options(items,pairs)
            return json.dumps(task_segment_payload([dict(i.payload(),document_name=names[i.document_id]) for i in items]),ensure_ascii=False,
                sort_keys=True,separators=(',',':'),allow_nan=False)
        items,truncated,chars,drops=pack_evidence(candidates,context_max_chars=budget.context_chars,
            serialize=serialize,max_items=budget.context_limit,slots=slots,
            partners=partners)
        assign_unit_options(items,pairs)
        pack=EvidencePack(items=items,candidates=candidates,index_id=None,parse_version_id=None,
            truncated=truncated,context_chars=chars,drop_reasons=drops,
            review_policy=packs[0].review_policy if packs else None,continuation_pairs=pairs,
            slot_hints=slot_hints(slots,{i.chunk_id for i in items},visible_ranges(items)),
            budget_profile=dict(task_type=query_plan['task_type'], **budget.__dict__),admission_groups=math_groups,
            math_hints=group_hints(math_groups,{i.chunk_id for i in candidates},{i.chunk_id for i in items}))
        pack.math_hints.update(blocked_count=sum(p.math_hints.get('blocked_count',0) for p in packs),
            unlinked_formula_count=sum(p.math_hints.get('unlinked_formula_count',0) for p in packs))
    for diagnostic in diagnostics:
        admitted = [i.chunk_id for i in items if i.document_id == diagnostic['document_id']]
        diagnostic['fusion'] = admission_report(diagnostic['fusion'], admitted, diagnostic.pop('_discard_details')+drops)
        diagnostic['admitted_count'] = len(admitted)
    complete = len(items)==sum(d['source_count'] for d in diagnostics) and all(
        i.read_ranges is None or i.read_ranges == [[0,len(i.text)]] for i in items)
    report=dict(version=PIPELINE_VERSION,budget=budget.__dict__,documents=diagnostics,
        admitted_count=len(items),context_chars=chars,truncated=truncated,drop_count=len(drops),
        query_plan=public_plan(query_plan), slots=pack.slot_hints,math_evidence=pack.math_hints,
        coverage=dict(route='complete_indexed_text' if complete else 'selected_evidence',
            indexed_chunk_count=sum(d['source_count'] for d in diagnostics), admitted_chunk_count=len(items),
            semantic_completeness_verified=False),
        input_manifest=[dict(document_id=i.document_id,parse_version_id=i.parse_version_id,chunk_id=i.chunk_id,
            text_sha256=sha256(i.text.encode()).hexdigest(),read_ranges=i.read_ranges) for i in items])
    return pack,report
