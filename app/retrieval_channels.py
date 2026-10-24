"""可回放的通道契约。分数只属于各自通道，不能用作事实正确概率。"""
from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re
from typing import Protocol

CHANNEL_VERSION = 'retrieval-channels-v1'


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ChannelResult:
    document_id: str
    parse_version_id: str
    chunk_id: str
    channel: str
    raw_score: float
    rank: int
    strategy_version: str

    def __post_init__(self):
        if (not all(isinstance(v, str) and v for v in (self.document_id, self.parse_version_id,
                self.chunk_id, self.channel, self.strategy_version)) or type(self.rank) is not int
                or self.rank < 1 or type(self.raw_score) not in (int, float)
                or not math.isfinite(self.raw_score)):
            raise ValueError('invalid_channel_result')


class Reranker(Protocol):
    version: str

    def rank(self, question: str, candidates: list[dict]) -> list[str]: ...


def apply_reranker(question, candidates, reranker=None, protected=()):
    """精排只能改变顺序，不能增删来源，也不能挤走受保护的证据组。"""
    if reranker is None:
        return candidates, {'enabled': False, 'supplier_calls': 0}
    original = [h['chunk_id'] for h in candidates]
    try:
        ranked = reranker.rank(question, [dict(h) for h in candidates])
        if (not isinstance(ranked, list) or len(ranked) != len(original)
                or len(set(ranked)) != len(ranked) or set(ranked) != set(original)):
            raise ValueError('reranker_scope_changed')
        locked = set(protected)
        ordering = [i for i in original if i in locked] + [i for i in ranked if i not in locked]
        by_id = {h['chunk_id']: h for h in candidates}
        return [by_id[i] for i in ordering], {'enabled': True, 'version': reranker.version,
                'state': 'applied', 'semantic_gain_verified': False}
    except (ValueError, TypeError, KeyError, OSError):
        return candidates, {'enabled': True, 'version': reranker.version, 'state': 'fallback_original',
                            'semantic_gain_verified': False}


def attach_channels(question, vector_result, chunks, planned, *, reranker=None):
    from app.retrieval_planning import _bm25, _search_text, RETRIEVAL_STRATEGY_VERSION
    pool = sorted(chunks, key=lambda c: (c.order_index if c.order_index is not None else 1 << 30, c.id))
    by_id = {c.id: c for c in pool}
    hits = []
    def add(identifier, channel, score, rank):
        c = by_id[identifier]
        hits.append(asdict(ChannelResult(c.document_id, c.parse_version_id, c.id, channel,
                                        score, rank, RETRIEVAL_STRATEGY_VERSION)))
    for rank, h in enumerate(vector_result.get('results', []), 1):
        if h['chunk_id'] not in by_id:
            raise ValueError('channel_scope_mismatch')
        add(h['chunk_id'], 'vector', h['score'], rank)
    for rank, (index, score) in enumerate(_bm25(_search_text(question), [_search_text(c.text) for c in pool]), 1):
        add(pool[index].id, 'lexical', score, rank)
    planning = planned.setdefault('planning', {})
    structural = []
    if planning.get('route')=='complete_indexed_text':
        structural.extend(h['chunk_id'] for h in planned['results'])
    for slot in planning.get('evidence_slots', []):
        structural.extend(slot.get('candidate_ids', []))
    for identifier, reason in planning.get('selection_reasons', {}).items():
        if reason not in {'coverage_rerank', 'vector_candidate_floor', 'vector', 'bm25'}:
            structural.append(identifier)
    for rank, identifier in enumerate(dict.fromkeys(structural), 1):
        if identifier in by_id:
            add(identifier, 'structural', 1.0, rank)
    initial = [h['chunk_id'] for h in planned['results']]
    protected = list(dict.fromkeys(i for slot in planning.get('evidence_slots', []) for i in slot.get('candidate_ids', [])[:2]))
    protected += [i for g in planning.get('math_evidence', {}).get('groups', []) for i in g.get('chunk_ids', [])]
    planned['results'], rerank = apply_reranker(question, planned['results'], reranker, protected)
    final = [h['chunk_id'] for h in planned['results']]
    planned['evidence_order'] = final
    grouped = {}
    for hit in hits:
        grouped.setdefault(hit['chunk_id'], []).append(hit)
    # 明细只记录本次候选；其余淘汰以总数报告，避免每轮复制整库原文。
    planning['channel_contract'] = {
        'version': CHANNEL_VERSION, 'channel_hits': dict(Counter(h['channel'] for h in hits)),
        'unique_recalled_count': len(grouped), 'rank_excluded_count': len(set(grouped) - set(final)),
        'selection_reasons': dict(Counter(planning.get('selection_reasons', {}).values())),
        'candidates': [{'document_id': by_id[i].document_id, 'parse_version_id': by_id[i].parse_version_id,
            'chunk_id': i, 'hits': grouped.get(i, []), 'rank_before': initial.index(i) + 1,
            'rank_after': rank, 'selection_reason': planning.get('selection_reasons', {}).get(i, 'full_text')}
            for rank, i in enumerate(final, 1)], 'reranker': rerank,
        'input_hash': fingerprint({'question': question, 'vector': vector_result,
            'chunks': [c.model_dump() for c in pool]}),
        'semantic_completeness_verified': False}
    return planned


class ReplayReranker:
    """只使用精确冻结输入的离线排序；不存在匹配时回到原排序，不外调。"""
    version = 'frozen-reranker-replay-v1'

    def __init__(self, path):
        self.path = path

    def rank(self, question, candidates):
        with open(self.path, encoding='utf-8') as source:
            replay = json.load(source)
        key = fingerprint({'question': question, 'candidates': candidates})
        if replay.get('version') != self.version or key not in replay.get('rankings', {}):
            raise ValueError('replay_input_mismatch')
        return replay['rankings'][key]


def configured_reranker(settings):
    return ReplayReranker(settings.reranker_replay_path) if getattr(settings, 'reranker_replay_path', '') else None


def admission_report(contract, admitted_ids, discarded=()):
    selected = {c['chunk_id'] for c in contract.get('candidates', [])}
    lost=selected-set(admitted_ids)
    reasons={}
    for message in discarded:
        if not isinstance(message,str):continue
        code=('quality' if '不可读' in message or '没有正文' in message else
              'threshold' if '分值' in message else 'duplicate' if '重复' in message or '去重' in message else
              'incomplete_group' if '缺少成员' in message else 'budget' if any(t in message for t in ('预算','上限','超出')) else 'unattributed')
        for identifier in lost:
            if re.search(r'chunk_id='+re.escape(identifier)+r'(?:\s|，|的|$)',message):
                reasons[identifier]=code
    counts=Counter(reasons.get(identifier,'unattributed') for identifier in lost)
    return {**contract, 'admitted_count': len(selected & set(admitted_ids)),
            'no_candidate':contract.get('unique_recalled_count',0)==0,
            'budget_or_quality_excluded_count': len(lost),
            'quality_excluded_count':counts['quality'],'budget_excluded_count':counts['budget'],
            'threshold_excluded_count':counts['threshold'],'duplicate_excluded_count':counts['duplicate'],
            'group_excluded_count':counts['incomplete_group'],'unattributed_excluded_count':counts['unattributed'],
            'discard_reasons':dict(counts)}
