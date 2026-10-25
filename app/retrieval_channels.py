"""可回放的通道契约。分数只属于各自通道，不能用作事实正确概率。"""
from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re
from typing import Protocol
from copy import deepcopy

CHANNEL_VERSION = 'retrieval-channels-v2'


class Discard(str):
    """兼容旧文字显示，机器归因使用结构字段而非中文匹配。"""
    def __new__(cls, chunk_id, code, message):
        value = super().__new__(cls, message)
        value.chunk_id, value.code = chunk_id, code
        return value


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
    query_id: str = 'original'
    score_kind: str = 'channel_score'

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
        ranked = reranker.rank(question, deepcopy(candidates))
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


def collect_channels(question, vector_result, chunks):
    """融合前生成实际输入；全问、子问和标题的原始分数分别保留。"""
    from app.retrieval_planning import _bm25, _search_text, _heading_views, retrieval_aspects, RETRIEVAL_STRATEGY_VERSION
    pool = sorted(chunks, key=lambda c: (c.order_index if c.order_index is not None else 1 << 30, c.id))
    by_id = {c.id: c for c in pool}
    hits = []
    def add(identifier, channel, score, rank, query_id='original', score_kind='channel_score'):
        c = by_id[identifier]
        hits.append(asdict(ChannelResult(c.document_id, c.parse_version_id, c.id, channel,
                                        score, rank, RETRIEVAL_STRATEGY_VERSION, query_id, score_kind)))
    vector_hits = {h['chunk_id']: h for h in vector_result.get('results', [])}
    rows = [dict(vector_hits.get(c.id) or dict(chunk_id=c.id,document_id=c.document_id,parse_version_id=c.parse_version_id,
        text=c.text,page=c.page,score=vector_result.get('candidate_scores',{}).get(c.id,0.0),chunk_type=c.chunk_type,
        heading_path=c.heading_path,order_index=c.order_index,block_id=c.block_id,sources=[s.model_dump() for s in c.sources])) for c in pool]
    texts = [_search_text(c.text) for c in pool]
    headings, derived = _heading_views(rows)
    aspects = retrieval_aspects(_search_text(question))
    for rank, h in enumerate(vector_result.get('results', []), 1):
        if h['chunk_id'] not in by_id:
            raise ValueError('channel_scope_mismatch')
        add(h['chunk_id'], 'vector', h['score'], rank)
    for query_id, query in [('original',_search_text(question)), *[(a['id'],a['query']) for a in aspects]]:
        for rank, (index, score) in enumerate(_bm25(query, texts), 1):
            add(pool[index].id, 'lexical', score, rank, query_id)
        for rank, (index, score) in enumerate(_bm25(query, headings), 1):
            add(pool[index].id, 'structural', score, rank, 'heading:'+query_id, 'heading_bm25')
    return {'hits':hits,'pool':rows,'aspects':aspects,'headings':headings,'derived_heading_count':derived}


def scores(channels, channel, query_id):
    indexes = {h['chunk_id']:i for i,h in enumerate(channels['pool'])}
    return {indexes[h['chunk_id']]:h['raw_score'] for h in channels['hits'] if h['channel']==channel and h['query_id']==query_id}


def structural_hit(channels, identifier, reason):
    from app.retrieval_planning import RETRIEVAL_STRATEGY_VERSION
    if any(h['chunk_id']==identifier and h['query_id']=='rule:'+reason for h in channels['hits']):return
    item = next(h for h in channels['pool'] if h['chunk_id']==identifier)
    rank = 1 + sum(h['channel']=='structural' and h['query_id']=='rule:'+reason for h in channels['hits'])
    channels['hits'].append(asdict(ChannelResult(item['document_id'],item['parse_version_id'],identifier,
        'structural',0.0,rank,RETRIEVAL_STRATEGY_VERSION,'rule:'+reason,'rule_membership')))


def attach_channels(question, vector_result, chunks, planned, *, reranker=None, channels=None):
    channels = channels or collect_channels(question, vector_result, chunks)
    pool = sorted(chunks, key=lambda c: (c.order_index if c.order_index is not None else 1 << 30, c.id))
    by_id = {c.id:c for c in pool}
    hits = channels['hits']
    planning = planned.setdefault('planning', {})
    if planning.get('route')=='complete_indexed_text':
        for hit in planned['results']:structural_hit(channels,hit['chunk_id'],'complete_indexed_text')
    initial = [h['chunk_id'] for h in planned['results']]
    protected = list(dict.fromkeys(i for slot in planning.get('evidence_slots', []) for i in slot.get('candidate_ids', [])[:2]))
    protected += [i for g in planning.get('math_evidence', {}).get('groups', []) for i in g.get('chunk_ids', [])]
    planned['results'], rerank = apply_reranker(question, planned['results'], reranker, protected)
    final = [h['chunk_id'] for h in planned['results']]
    planned['evidence_order'] = final
    grouped = {}
    for hit in hits:
        grouped.setdefault(hit['chunk_id'], []).append(hit)
    # 保存淘汰前身份而不复制原文；每阶段集合来自实际执行时点。
    planning['channel_contract'] = {
        'version': CHANNEL_VERSION, 'channel_hits': dict(Counter(h['channel'] for h in hits)),
        'unique_recalled_count': len(grouped), 'rank_excluded_count': len(set(grouped) - set(final)),
        'recalled_ids':list(grouped),'ranked_ids':final,
        'recalled':[{'document_id':by_id[i].document_id,'parse_version_id':by_id[i].parse_version_id,
                     'chunk_id':i,'hits':h} for i,h in grouped.items()],
        'selection_reasons': dict(Counter(planning.get('selection_reasons', {}).values())),
        'candidates': [{'document_id': by_id[i].document_id, 'parse_version_id': by_id[i].parse_version_id,
            'chunk_id': i, 'hits': grouped.get(i, []), 'rank_before': initial.index(i) + 1,
            'rank_after': rank, 'selection_reason': planning.get('selection_reasons', {}).get(i, 'full_text')}
            for rank, i in enumerate(final, 1)], 'reranker': rerank,
        'input_hash': fingerprint({'question': question, 'vector': {k:v for k,v in vector_result.items() if k not in ('timings_ms','vector_decode_cache_hit')},
            'chunks': [c.model_dump() for c in pool]}),
        'semantic_completeness_verified': False}
    return planned


class ReplayReranker:
    """只使用精确冻结输入的离线排序；不存在匹配时回到原排序，不外调。"""
    version = 'frozen-reranker-replay-v1'

    def __init__(self, path=None, expected_hash=None, *, content=None):
        self.path = path
        from pathlib import Path
        content=Path(path).read_bytes() if content is None else content
        self.content=content
        self.content_hash=sha256(content).hexdigest()
        if expected_hash and self.content_hash!=expected_hash:raise ValueError('replay_input_changed')
        self.replay=json.loads(content)

    def rank(self, question, candidates):
        replay = self.replay
        key = fingerprint({'question': question, 'candidates': candidates})
        if replay.get('version') != self.version or key not in replay.get('rankings', {}):
            raise ValueError('replay_input_mismatch')
        return replay['rankings'][key]


def configured_reranker(settings, expected_hash=None):
    return ReplayReranker(settings.reranker_replay_path,expected_hash) if getattr(settings, 'reranker_replay_path', '') else None


def admission_report(contract, admitted_ids, discarded=()):
    selected = {c['chunk_id'] for c in contract.get('candidates', [])}
    lost=selected-set(admitted_ids)
    reasons={}
    for message in discarded:
        if isinstance(message,dict):
            if message.get('chunk_id') in lost:reasons[message['chunk_id']]=message['code']
            continue
        if hasattr(message,'code'):
            if message.chunk_id in lost:reasons[message.chunk_id]=message.code
            continue
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
