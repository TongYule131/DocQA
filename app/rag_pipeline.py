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

PIPELINE_VERSION = 'grounded-retrieval-pipeline-v1'


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
                      *, check=lambda: None, observe=lambda *args: nullcontext()):
    """所有通道只读同一冻结版本；观察器只记时，不记录用户正文。"""
    packs, diagnostics = [], []
    for doc in documents:
        check()
        with observe('向量召回', 'RETRIEVE_VECTOR'):
            found = index.search_snapshot(doc['document_id'], search_question, budget.recall_limit,
                snapshot=doc['snapshot'], include_scores=True)
        chunks = repository.chunks(doc['document_id'], doc['snapshot']['parse_version_id'])
        with observe('关键词、结构线索与融合重排', 'RETRIEVE_FUSION'):
            planned = plan_retrieval(question, found, chunks, max_candidates=budget.candidate_limit,
                fulltext_budget=budget.context_chars, max_items=budget.context_limit,
                payload_transform=task_segment_payload)
        with observe('原文范围核对与抽取压缩', 'EVIDENCE'):
            built = build_evidence(planned, settings, expected_document_id=doc['document_id'],
                max_items=budget.context_limit, context_max_chars=budget.context_chars,
                payload_transform=task_segment_payload,
                table_blocks=repository.blocks(doc['snapshot']['parse_version_id']), extractive_question=question)
            if built.fatal_reason: raise PipelineError('source_scope_invalid')
        packs.append(built.pack)
        diagnostics.append(dict(document_id=doc['document_id'],parse_version_id=doc['snapshot']['parse_version_id'],
            source_count=len(chunks),vector_count=len(found.get('results',[])),
            candidate_count=len(planned.get('results',[])),admitted_count=len(built.pack.items),
            channels=dict(Counter(planned.get('planning',{}).get('selection_reasons',{}).values())),
            numeric_slots=planned.get('planning',{}).get('numeric_slot_count',0)))
    with observe('跨资料去重与上下文预算装入', 'CONTEXT_PACK'):
        packs.sort(key=lambda p:max((i.score for i in p.items),default=0),reverse=True)
        candidates, seen = [], set()
        for n in range(max((len(p.items) for p in packs),default=0)):
            for pack in packs:
                if n >= len(pack.items): continue
                item = pack.items[n]; key=(item.document_id,item.parse_version_id,item.chunk_id)
                if key not in seen: candidates.append(item); seen.add(key)
        pairs=[pair for pack in packs for pair in pack.continuation_pairs]
        groups={i.chunk_id:{i.chunk_id} for i in candidates}
        for pair in pairs:
            a,b=pair['left_chunk_id'],pair['right_chunk_id']
            if a in groups and b in groups:
                union=groups[a]|groups[b]
                for key in union: groups[key]=union
        order={i.chunk_id:n for n,i in enumerate(candidates)}
        def serialize(items):
            assign_unit_options(items,pairs)
            return json.dumps(task_segment_payload([i.payload() for i in items]),ensure_ascii=False,
                sort_keys=True,separators=(',',':'),allow_nan=False)
        items,truncated,chars,drops=pack_evidence(candidates,context_max_chars=budget.context_chars,
            serialize=serialize,max_items=budget.context_limit,
            partners={k:sorted(v,key=order.get) for k,v in groups.items()})
        assign_unit_options(items,pairs)
        pack=EvidencePack(items=items,candidates=candidates,index_id=None,parse_version_id=None,
            truncated=truncated,context_chars=chars,drop_reasons=drops,
            review_policy=packs[0].review_policy if packs else None,continuation_pairs=pairs)
    report=dict(version=PIPELINE_VERSION,budget=budget.__dict__,documents=diagnostics,
        admitted_count=len(items),context_chars=chars,truncated=truncated,drop_count=len(drops),
        input_manifest=[dict(document_id=i.document_id,parse_version_id=i.parse_version_id,chunk_id=i.chunk_id,
            text_sha256=sha256(i.text.encode()).hexdigest(),read_ranges=i.read_ranges) for i in items])
    return pack,report
