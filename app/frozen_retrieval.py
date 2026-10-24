"""离线冻结候选复放。文件含用户原文时仅留本机，不进入 Git 或 CI 工件。"""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.rag_pipeline import knowledge_context, RetrievalBudget
from app.retrieval_channels import fingerprint
from app.schemas import Chunk, Block

VERSION = 'frozen-retrieval-suite-v1'


def freeze(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('empty_frozen_suite')
    for case in cases:
        if not all(key in case for key in ('id', 'question', 'documents', 'budget', 'required_groups', 'query_vector')):
            raise ValueError('incomplete_frozen_case')
        if case.get('annotation_status') not in ('synthetic', 'reviewed', 'unreviewed'):
            raise ValueError('annotation_status_required')
        if not case.get('document_group'):
            raise ValueError('document_group_required')
        if (not isinstance(case['query_vector'],list) or not case['query_vector']
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in case['query_vector'])):
            raise ValueError('frozen_query_vector_invalid')
    return {'version': VERSION, 'cases': cases, 'input_hash': fingerprint(cases)}


def evaluate(suite, settings=None):
    if suite.get('version') != VERSION or fingerprint(suite['cases']) != suite.get('input_hash'):
        raise ValueError('frozen_suite_changed')
    settings = settings or Settings()
    records = []
    for case in suite['cases']:
        chunks, blocks, sources = {}, {}, {}
        seen_chunks=set()
        for doc in case['documents']:
            snapshot = doc['snapshot']
            version = snapshot['parse_version_id']
            chunks[(doc['document_id'], version)] = [Chunk.model_validate(c) for c in doc['chunks']]
            by_id={c.id:c for c in chunks[(doc['document_id'],version)]}
            if seen_chunks.intersection(by_id):raise ValueError('duplicate_frozen_chunk_identity')
            seen_chunks.update(by_id)
            if any(c.document_id != doc['document_id'] or c.parse_version_id != version for c in chunks[(doc['document_id'], version)]):
                raise ValueError('frozen_source_scope_mismatch')
            blocks[version] = [Block.model_validate(b) for b in doc.get('blocks', [])]
            if not snapshot.get('index_id') or not snapshot.get('source_signature'):
                raise ValueError('frozen_index_identity_missing')
            for hit in doc['vector_result'].get('results',[]):
                c=by_id.get(hit.get('chunk_id'))
                if (not c or hit.get('document_id')!=c.document_id or hit.get('parse_version_id')!=c.parse_version_id
                        or hit.get('text')!=c.text or hit.get('sources')!=[s.model_dump() for s in c.sources]):
                    raise ValueError('frozen_candidate_source_changed')
            sources[doc['document_id']] = doc
        class Repository:
            def chunks(self, identifier, version):return chunks[(identifier, version)]
            def blocks(self, version):return blocks.get(version, [])
            def get_parse_version(self, version):return SimpleNamespace(quality_status='warnings',warnings=[])
        class Index:
            def search_snapshot(self, identifier, question, limit, *, snapshot, include_scores):
                if snapshot != sources[identifier]['snapshot']:
                    raise ValueError('frozen_snapshot_changed')
                return sources[identifier]['vector_result']
        pack, report = knowledge_context(Index(), Repository(), case['documents'], case['question'],
            case['question'], settings, RetrievalBudget(**case['budget']))
        candidates = {c['chunk_id'] for d in report['documents'] for c in d['fusion']['candidates']}
        admitted = {i.chunk_id for i in pack.items}
        visible = {i.chunk_id: (i.read_ranges or [[0, len(i.text)]]) for i in pack.items}
        quoted = set(case.get('validated_quote_chunk_ids', [])) if 'validated_quote_chunk_ids' in case else None
        def survives(group, stage):
            return all(i in stage for i in group['all_of'])
        def visible_group(group):
            if not survives(group, admitted):return False
            return all(any(left <= start and end <= right for left, right in visible.get(identifier, []))
                       for identifier, spans in group.get('required_ranges', {}).items() for start, end in spans)
        groups = case['required_groups']
        records.append({'id': case['id'], 'document_group': case['document_group'],
            'annotation_status': case['annotation_status'], 'group_count': len(groups),
            'candidate_groups': sum(survives(g, candidates) for g in groups),
            'post_rank_groups': sum(survives(g, candidates) for g in groups),
            'admitted_groups': sum(survives(g, admitted) for g in groups),
            'visible_groups': sum(visible_group(g) for g in groups),
            'quoted_groups': sum(survives(g, quoted) for g in groups) if quoted is not None else None,
            'input_manifest': report['input_manifest'], 'semantic_gain_verified': False})
    grouped = defaultdict(list)
    for record in records:grouped[record['document_group']].append(record['id'])
    return {'version': VERSION, 'input_hash': suite['input_hash'], 'cases': records,
            'document_groups': dict(grouped), 'provider_calls': 0, 'semantic_gain_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('freeze', 'evaluate'))
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    value = json.loads(args.input.read_text(encoding='utf-8'))
    result = freeze(value) if args.operation == 'freeze' else evaluate(value)
    # 排序报告含身份和原文哈希，仍是本机私有证据，调用方明确指定输出路径。
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({'cases': len(result['cases']), 'provider_calls': 0}))


if __name__ == '__main__':main()
