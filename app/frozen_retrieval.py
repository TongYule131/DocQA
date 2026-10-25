"""来源、向量、质量及策略完整冻结；只在本机保存含原文的复放包。"""
import argparse
import base64
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.embedding import normalize_vector
from app.rag_pipeline import knowledge_context, RetrievalBudget
from app.retrieval_channels import fingerprint
from app.schemas import Chunk, Block, QualityWarning

VERSION = 'frozen-retrieval-suite-v3'
SETTING_NAMES = ('rag_min_score','question_candidate_k','question_context_k','question_context_max_chars',
    'question_input_max_chars','rag_retrieval_k','rag_context_k','rag_context_max_chars','rag_input_max_chars',
    'question_retrieval_strategy','query_term_mapping','query_planner_enabled','memory_summary_enabled','reranker_replay_path')


def settings_input(settings):
    return {name:getattr(settings,name) for name in SETTING_NAMES}


def strategy_input():
    from app.retrieval_planning import RETRIEVAL_STRATEGY_VERSION
    from app.rag_pipeline import PIPELINE_VERSION
    from app.retrieval_channels import CHANNEL_VERSION
    from app.rag_context import EVIDENCE_PACKING_VERSION
    folder=Path(__file__).parent
    names=['retrieval_planning.py','retrieval_channels.py','rag_pipeline.py','rag_context.py',
        'rag_extract_context.py','rag_evidence_slots.py','rag_slot_ranges.py','rag_math_evidence.py',
        'rag_numeric_roles.py','query_planning.py','quote_spans.py','frozen_retrieval.py']
    return {'retrieval':RETRIEVAL_STRATEGY_VERSION,'pipeline':PIPELINE_VERSION,'channels':CHANNEL_VERSION,
        'packing':EVIDENCE_PACKING_VERSION,'source_hashes':{n:sha256((folder/n).read_bytes()).hexdigest() for n in names}}


def vector_results(doc, query, limit):
    normalize_vector(query)
    q=query
    if abs(sum(v*v for v in q)-1)>1e-6:raise ValueError('frozen_query_vector_not_normalized')
    vectors={r['chunk_id']:normalize_vector(r['vector']) for r in doc['vectors']}
    chunks=[Chunk.model_validate(c) for c in doc['chunks']]
    if len(vectors)!=len(doc['vectors']) or set(vectors)!={c.id for c in chunks}:raise ValueError('frozen_vectors_incomplete')
    results=[]
    for c in chunks:
        v=vectors[c.id]
        if len(v)!=len(q):raise ValueError('frozen_vector_dimension_changed')
        score=max(-1.0,min(1.0,sum(a*b for a,b in zip(q,v))))
        results.append(dict(chunk_id=c.id,document_id=c.document_id,parse_version_id=c.parse_version_id,
            page=c.page,text=c.text,score=score,chunk_type=c.chunk_type,heading_path=c.heading_path,
            order_index=c.order_index,block_id=c.block_id,sources=[s.model_dump() for s in c.sources]))
    results.sort(key=lambda h:(-h['score'],h['order_index'] if h['order_index'] is not None else 1<<30,h['chunk_id']))
    return {**{k:v for k,v in doc.get('vector_result',{}).items() if k not in ('timings_ms','vector_decode_cache_hit')},
        'index_id':doc['snapshot']['index_id'],'parse_version_id':doc['snapshot']['parse_version_id'],
        'candidate_scores':{h['chunk_id']:h['score'] for h in results},'results':results[:limit]}


def validate_case(case):
    from app.vector_index import compute_signature, evidence_signature
    if not all(k in case for k in ('id','question','documents','budget','required_groups','query_vector','document_group','annotation_status')):
        raise ValueError('incomplete_frozen_case')
    if case['annotation_status'] not in ('synthetic','developer_reviewed','reviewed','unreviewed') or not case['document_group']:
        raise ValueError('annotation_status_required')
    RetrievalBudget(**case['budget']);normalize_vector(case['query_vector'])
    all_chunks={}
    for doc in case['documents']:
        if not all(k in doc for k in ('vectors','quality','chunks','snapshot')):raise ValueError('incomplete_frozen_document')
        chunks=[Chunk.model_validate(c) for c in doc['chunks']];snapshot=doc['snapshot'];version=snapshot['parse_version_id']
        if not snapshot.get('index_id') or not snapshot.get('provider_signature'):raise ValueError('frozen_index_identity_missing')
        if any(c.document_id!=doc['document_id'] or c.parse_version_id!=version or c.id in all_chunks for c in chunks):raise ValueError('frozen_source_scope_mismatch')
        if len({c.id for c in chunks})!=len(chunks):raise ValueError('duplicate_frozen_chunk_identity')
        if (compute_signature(chunks,version,snapshot['source_signature_algo'])!=snapshot['source_signature']
            or evidence_signature(chunks)!=snapshot['evidence_signature']):raise ValueError('frozen_candidate_source_changed')
        if doc['quality']['quality_status'] not in ('ok','warnings','invalid'):raise ValueError('frozen_quality_invalid')
        for warning in doc['quality']['warnings']:QualityWarning.model_validate(warning)
        replay=vector_results(doc,case['query_vector'],case['budget']['recall_limit']);saved=doc['vector_result']
        if (saved['results']!=replay['results'][:len(saved['results'])]
            or (saved.get('candidate_scores') is not None and saved['candidate_scores']!=replay['candidate_scores'])):raise ValueError('frozen_vector_result_changed')
        all_chunks.update({c.id:c for c in chunks})
    if not isinstance(case['required_groups'],list):raise ValueError('frozen_groups_invalid')
    for group in case['required_groups']:
        if not isinstance(group.get('all_of'),list) or not group['all_of'] or len(set(group['all_of']))!=len(group['all_of']):raise ValueError('frozen_group_empty_or_duplicate')
        if not set(group['all_of'])<=all_chunks.keys():raise ValueError('frozen_group_unknown_chunk')
        for identifier,spans in group.get('required_ranges',{}).items():
            if identifier not in group['all_of'] or not spans:raise ValueError('frozen_group_range_invalid')
            for span in spans:
                if (not isinstance(span,list) or len(span)!=2 or any(type(n) is not int for n in span)
                    or not 0<=span[0]<span[1]<=len(all_chunks[identifier].text)):raise ValueError('frozen_group_range_invalid')
    return all_chunks


def freeze(cases, settings=None):
    if not isinstance(cases,list) or not cases:raise ValueError('empty_frozen_suite')
    if len({c['id'] for c in cases})!=len(cases):raise ValueError('duplicate_frozen_case')
    for case in cases:validate_case(case)
    from app.retrieval_channels import configured_reranker
    settings=settings or Settings();adapter=configured_reranker(settings)
    replay={'sha256':adapter.content_hash,'content_base64':base64.b64encode(adapter.content).decode('ascii')} if adapter else None
    data={'version':VERSION,'cases':cases,'settings':settings_input(settings),'strategy':strategy_input(),'reranker':replay}
    return {**data,'input_hash':fingerprint(data)}


def evaluate(suite, settings=None, *, allow_strategy_change=False):
    frozen={k:v for k,v in suite.items() if k!='input_hash'}
    if suite.get('version')!=VERSION or fingerprint(frozen)!=suite.get('input_hash'):raise ValueError('frozen_suite_changed')
    current=strategy_input()
    if not allow_strategy_change and suite['strategy']!=current:raise ValueError('frozen_strategy_changed')
    settings=settings or Settings(**suite['settings'])
    if settings_input(settings)!=suite['settings']:raise ValueError('frozen_settings_changed')
    from app.retrieval_channels import ReplayReranker
    replay=suite.get('reranker');adapter=None
    if bool(replay)!=bool(settings.reranker_replay_path):raise ValueError('frozen_reranker_missing')
    if replay:
        adapter=ReplayReranker(expected_hash=replay['sha256'],content=base64.b64decode(replay['content_base64'],validate=True))
    records=[]
    for case in suite['cases']:
        by_id=validate_case(case);documents={d['document_id']:d for d in case['documents']}
        versions={d['snapshot']['parse_version_id']:d for d in case['documents']}
        class Repository:
            def chunks(self,identifier,version):return [Chunk.model_validate(c) for c in documents[identifier]['chunks'] if c['parse_version_id']==version]
            def blocks(self,version):return [Block.model_validate(b) for b in versions[version].get('blocks',[])]
            def get_parse_version(self,version):
                q=versions[version]['quality'];return SimpleNamespace(quality_status=q['quality_status'],warnings=[QualityWarning.model_validate(w) for w in q['warnings']])
        class Index:
            def search_snapshot(self,identifier,question,limit,*,snapshot,include_scores):
                if snapshot!=documents[identifier]['snapshot']:raise ValueError('frozen_snapshot_changed')
                return vector_results(documents[identifier],case['query_vector'],limit)
        pack,report=knowledge_context(Index(),Repository(),case['documents'],case['question'],case.get('search_question',case['question']),settings,RetrievalBudget(**case['budget']),reranker_adapter=adapter,
            query_plan=case.get('query_input',{}).get('query_plan'),planning_question=case.get('planning_question'))
        recalled={i for d in report['documents'] for i in d['fusion']['recalled_ids']}
        ranked={i for d in report['documents'] for i in d['fusion']['ranked_ids']}
        admitted={i.chunk_id for i in pack.items}
        visible={i.chunk_id:(i.read_ranges if i.read_ranges is not None else [[0,len(i.text)]]) for i in pack.items}
        quotes=defaultdict(list)
        for item in case.get('validated_quotes',[]):
            identifier=item['chunk_id'];start,end=item['start'],item['end']
            if (identifier not in visible or type(start) is not int or type(end) is not int or not 0<=start<end<=len(by_id[identifier].text)
                or by_id[identifier].text[start:end]!=item['quote'] or not any(left<=start<end<=right for left,right in visible[identifier])):raise ValueError('frozen_quote_not_visible_or_valid')
            quotes[identifier].append([start,end])
        def survives(group,ids):return all(i in ids for i in group['all_of'])
        def supports(group,ranges):
            return survives(group,ranges) and all(any(left<=start<end<=right for left,right in ranges[identifier])
                for identifier,spans in group.get('required_ranges',{}).items() for start,end in spans)
        groups=case['required_groups'];states=[]
        for group in groups:
            states.append({'recalled':survives(group,recalled),'ranked':survives(group,ranked),'admitted':survives(group,admitted),
                'visible':supports(group,visible),'quoted':supports(group,quotes) if 'validated_quotes' in case else None})
        records.append({'id':case['id'],'document_group':case['document_group'],'annotation_status':case['annotation_status'],'group_count':len(groups),
            'candidate_groups':sum(s['recalled'] for s in states),'post_rank_groups':sum(s['ranked'] for s in states),
            'admitted_groups':sum(s['admitted'] for s in states),'visible_groups':sum(s['visible'] for s in states),
            'quoted_groups':sum(s['quoted'] for s in states) if 'validated_quotes' in case else None,
            'group_states':states,'input_manifest':report['input_manifest'],'semantic_gain_verified':False})
    grouped=defaultdict(list)
    for record in records:grouped[record['document_group']].append(record['id'])
    return {'version':VERSION,'input_hash':suite['input_hash'],'evaluation_identity':fingerprint([suite['input_hash'],current,settings_input(settings)]),
        'strategy':current,'strategy_changed':current!=suite['strategy'],'cases':records,'document_groups':dict(grouped),
        'provider_calls':0,'query_vector_verified':True,'semantic_gain_verified':False}


def export_case(repository, task_id, *, required_groups, document_group, annotation_status='unreviewed'):
    """仅导出已有向量；调用者须先核对用户和任务权限。"""
    from app.knowledge_tasks import KnowledgeTasks
    tasks=KnowledgeTasks(repository);task=tasks.row(task_id)
    if not task:raise ValueError('task_missing')
    payload=json.loads(task['input_json']);vector=tasks.checkpoint(task_id,'query_vector')
    query_input=tasks.checkpoint(task_id,'query_input')
    if not vector or payload['route']['kind']!='KB':raise ValueError('task_has_no_frozen_query_vector')
    docs=[]
    for doc in payload['documents']:
        version=doc['snapshot']['parse_version_id'];quality=repository.get_parse_version(version)
        with repository.connect() as db:
            vectors=[{'chunk_id':r['chunk_id'],'vector':json.loads(r['vector'])} for r in db.execute('SELECT chunk_id,vector FROM embedding_vectors_v2 WHERE index_id=? ORDER BY chunk_id',(doc['snapshot']['index_id'],))]
        value={**doc,'chunks':[c.model_dump() for c in repository.chunks(doc['document_id'],version)],
            'blocks':[b.model_dump() for b in repository.blocks(version)],'vectors':vectors,
            'quality':{'quality_status':quality.quality_status,'warnings':[w.model_dump() for w in quality.warnings]}}
        value['vector_result']=vector_results(value,vector[0],payload['retrieval_budget']['recall_limit']);docs.append(value)
    if not query_input:raise ValueError('task_query_input_not_frozen')
    return {'id':task_id,'question':payload['question'],'search_question':query_input['search_question'],
        'planning_question':query_input.get('planning_question',payload.get('understanding',{}).get('resolved_question',payload['question'])),
        'documents':docs,'budget':payload['retrieval_budget'],'query_vector':vector[0],'required_groups':required_groups,
        'document_group':document_group,'annotation_status':annotation_status,'task_input_hash':task['input_hash'],'query_input':query_input}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=('freeze','evaluate','export'))
    parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path);parser.add_argument('--task-id');parser.add_argument('--owner-user-id')
    args=parser.parse_args(argv);value=json.loads(args.input.read_text(encoding='utf-8'))
    if args.operation=='export':
        if not args.data_dir or not args.task_id:parser.error('export requires --data-dir and --task-id; input is the annotation JSON')
        from contextlib import contextmanager
        import sqlite3
        from app.repository import Repository
        class ReadOnlyRepository(Repository):
            @contextmanager
            def connect(self):
                db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                try:yield db
                finally:db.close()
        repository=ReadOnlyRepository(args.data_dir/'docqa.db')
        with repository.connect() as db:owner=db.execute('SELECT owner_user_id FROM kb_tasks WHERE id=?',(args.task_id,)).fetchone()
        if not owner or owner[0]!=args.owner_user_id:raise ValueError('export_task_owner_mismatch')
        result=export_case(repository,args.task_id,required_groups=value['required_groups'],document_group=value['document_group'],annotation_status=value.get('annotation_status','unreviewed'))
    else:result=freeze(value) if args.operation=='freeze' else evaluate(value)
    with args.output.open('x',encoding='utf-8') as target:json.dump(result,target,ensure_ascii=False,indent=2)
    print(json.dumps({'cases':len(result['cases']) if 'cases' in result else 1,'provider_calls':0}))


if __name__=='__main__':main()
