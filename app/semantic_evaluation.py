"""意图校准与验收分开冻结；只读取保存向量，不调用模型或写入业务策略。"""
from contextlib import contextmanager,closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import argparse,json,sqlite3,unicodedata

from app.conversation_memory import valid_entity_context
from app.embedding import normalize_vector,EmbeddingError
from app.intent_routing import eligible_rows,resolve_understood_route
from app.query_understanding import understand
from app.retrieval_channels import fingerprint
from app.runtime import source_identity
from app.semantic_intents import SemanticIntents,configuration,rank_cached_examples

VERSION='intent-evaluation-suite-v1'
LABEL_KEYS=('id','question','expected_code','expected_kind','split','document_group','tag','scope','base_id','context')


def query_identity(text):
    value=unicodedata.normalize('NFKC',text).casefold()
    return ''.join(c for c in value if not c.isspace() and not unicodedata.category(c).startswith('P'))


def _identity(suite):return fingerprint({k:v for k,v in suite.items() if k!='input_hash'})


def _validate_inputs(suite):
    rows,cache,cases=suite['nodes'],suite['example_vectors'],suite['cases']
    if not isinstance(suite['provider_signature'],str) or not suite['provider_signature']:raise ValueError('provider_signature_required')
    if not isinstance(suite['allowed_tool_ids'],list) or any(not isinstance(i,str) or not i for i in suite['allowed_tool_ids']):raise ValueError('invalid_allowed_tool_ids')
    if not isinstance(cases,list) or not 12<=len(cases)<=2000:raise ValueError('need_calibration_and_holdout_cases')
    counts={split:0 for split in ('calibration','holdout')};groups={k:set() for k in counts};seen=set();ids=set()
    examples={query_identity(text) for row in rows for text in row['examples']}
    for case in cases:
        if (not isinstance(case,dict) or not all(k in case for k in LABEL_KEYS+('query_vector',))
            or case['split'] not in counts or not all(isinstance(case[k],str) and case[k].strip() for k in ('id','question','document_group','tag','expected_code'))
            or len(case['question'])>2000 or case['scope'] not in ('knowledge','document')
            or case['expected_kind'] not in ('KB','MCP','SYSTEM') or case['base_id'] is not None and not isinstance(case['base_id'],str)
            or not isinstance(case['context'],dict) or case['context'] and not valid_entity_context(case['context'])):
            raise ValueError('invalid_labeled_case')
        key=query_identity(case['question'])
        if case['id'] in ids or key in seen or key in examples:raise ValueError('duplicate_or_training_example_leak')
        ids.add(case['id']);seen.add(key);counts[case['split']]+=1;groups[case['split']].add(case['document_group'])
        eligible=eligible_rows(rows,case['base_id'],case['scope'])
        special={'knowledge-default':'KB','clarification-needed':'SYSTEM',
                 'live-source-unavailable':'SYSTEM','request-scope-needed':'SYSTEM'}
        expected=special.get(case['expected_code']) or next((r['kind'] for r in eligible if r['code']==case['expected_code']),None)
        if expected!=case['expected_kind']:raise ValueError('expected_route_outside_scope')
        # 冻结阶段只核对形状，不计算验收相似度或查看验收表现。
        if case['query_vector'] is not None:
            q=normalize_vector(case['query_vector'])
            for row in eligible:
                hashes={fingerprint_text(t) for t in row['examples']}
                if row['kind']=='SYSTEM' or not hashes:continue
                saved=cache.get(row['id'],[])
                if {v['example_hash'] for v in saved}!=hashes or len(saved)!=len(hashes):raise ValueError('semantic_examples_incomplete')
                if any(len(normalize_vector(v['vector']))!=len(q) for v in saved):raise ValueError('semantic_vector_dimension_mismatch')
    if min(counts.values())<6:raise ValueError('need_six_cases_per_split')
    if groups['calibration'] & groups['holdout']:raise ValueError('split_group_leak')
    for split in counts:
        selected=[c for c in cases if c['split']==split]
        if not any(c['expected_kind']=='KB' and c['expected_code']!='knowledge-default' for c in selected) or not any(c['expected_code']=='knowledge-default' for c in selected):
            raise ValueError('each_split_needs_business_and_fallback')


def fingerprint_text(text):
    from hashlib import sha256
    return sha256(text.encode()).hexdigest()


def freeze_suite(rows,cache,cases,signature,*,term_mapping='{}',allowed_tool_ids=(),annotation_reference=None):
    if not isinstance(signature,str) or not signature:raise ValueError('provider_signature_required')
    suite={'version':VERSION,'source_identity':source_identity(),'provider_signature':signature,
        'configuration':configuration(rows,signature),'nodes':deepcopy(rows),'example_vectors':deepcopy(cache),
        'cases':deepcopy(cases),'term_mapping':term_mapping,'allowed_tool_ids':sorted(set(allowed_tool_ids)),
        'annotation_reference':annotation_reference,'provider_calls':0}
    _validate_inputs(suite);suite['label_hash']=fingerprint([{k:c[k] for k in LABEL_KEYS} for c in cases]);suite['input_hash']=_identity(suite)
    return suite


def validate_suite(suite):
    if not isinstance(suite,dict) or suite.get('version')!=VERSION or suite.get('input_hash')!=_identity(suite):raise ValueError('suite_identity_mismatch')
    if suite['source_identity']!=source_identity():raise ValueError('suite_source_changed')
    if suite['configuration']!=configuration(suite['nodes'],suite['provider_signature']):raise ValueError('suite_configuration_changed')
    if suite['label_hash']!=fingerprint([{k:c[k] for k in LABEL_KEYS} for c in suite['cases']]):raise ValueError('suite_labels_changed')
    _validate_inputs(suite)


def _prepare_routes(suite,cases):
    prepared={}
    for case in cases:
        rows=eligible_rows(suite['nodes'],case['base_id'],case['scope'])
        understanding=understand(case['question'],case['context'],SimpleNamespace(query_term_mapping=suite['term_mapping']),scope=case['scope'])
        ranked=rank_cached_examples(rows,case['query_vector'],suite['example_vectors'],strict=True) if case['query_vector'] is not None else []
        prepared[case['id']]=(understanding,ranked)
    return prepared


def _evaluate(suite,cases,threshold,margin,prepared=None):
    if type(threshold) not in (float,int) or not 0<=threshold<=1 or type(margin) not in (float,int) or not 0<=margin<=1:raise ValueError('invalid_semantic_policy')
    outputs=[];safety={'business_diverted_to_system':0,'unconfigured_tool_route':0,'cross_base_route':0}
    if prepared is None:prepared=_prepare_routes(suite,cases)
    for case in cases:
        understanding,ranked=prepared[case['id']]
        value=resolve_understood_route(case['question'],case['base_id'],suite['nodes'],case['scope'],understanding,
            semantic_candidates=ranked,semantic_policy={'threshold':threshold,'margin':margin})
        route=value['route']
        if case['query_vector'] is None and route['kind']!='SYSTEM':raise ValueError('business_query_vector_missing')
        unsafe=[]
        if case['tag']=='mixed_business' and route['kind']=='SYSTEM' and (
            case['expected_kind']!='SYSTEM' or route['code'] not in ('live-source-unavailable','request-scope-needed','clarification-needed')):
            unsafe.append('business_diverted_to_system')
        if route['kind']=='MCP' and route.get('tool_id') not in suite['allowed_tool_ids']:unsafe.append('unconfigured_tool_route')
        if route['kind']!='SYSTEM' and route.get('base_id') not in (None,case['base_id']):unsafe.append('cross_base_route')
        for risk in unsafe:safety[risk]+=1
        outputs.append({'id':case['id'],'expected_code':case['expected_code'],'expected_kind':case['expected_kind'],
            'actual_code':route['code'],'actual_kind':route['kind'],'method':value['decision']['method'],'tag':case['tag'],
            'passed':(route['kind'],route['code'])==(case['expected_kind'],case['expected_code']),'safety_failures':unsafe})
    total=len(outputs);correct=sum(c['passed'] for c in outputs);minimum=24 if cases[0]['split']=='holdout' else 6
    return {'correct':correct,'total':total,'accuracy':correct/total,'safety':safety,'target':0.95,
        'accuracy_target_met':correct/total>=0.95,'minimum_cases':minimum,'sample_adequate':total>=minimum,
        'target_met':total>=minimum and correct/total>=0.95 and not any(safety.values()),'cases':outputs,
        'provider_calls':0,'tool_execution_performed':False,'independent_human_certification':False}


def select_policy(suite):
    validate_suite(suite);cases=[c for c in suite['cases'] if c['split']=='calibration'];choices=[];prepared=_prepare_routes(suite,cases)
    for threshold in (0.45,0.5,0.55,0.6,0.65,0.7,0.75,0.8,0.85,0.9,0.95):
        for margin in (0.05,0.1,0.15,0.2,0.25):
            report=_evaluate(suite,cases,threshold,margin,prepared)
            # 并列时采用预先固定的0.60/0.15邻近值，完全不读取验收分数。
            key=(-sum(report['safety'].values()),report['correct'],-abs(threshold-0.6),-abs(margin-0.15))
            choices.append((key,threshold,margin,report))
    _,threshold,margin,report=max(choices,key=lambda r:r[0])
    policy={'version':VERSION,'suite_input_hash':suite['input_hash'],'source_identity':suite['source_identity'],
        'calibration_case_hash':fingerprint(cases),'threshold':threshold,'margin':margin,'calibration':report,
        'holdout_used_for_selection':False,'selection_rule':'safety_then_correct_then_fixed_prior_060_015','provider_calls':0}
    policy['policy_hash']=fingerprint(policy);return policy


def evaluate_holdout(suite,policy):
    validate_suite(suite)
    if policy.get('policy_hash')!=fingerprint({k:v for k,v in policy.items() if k!='policy_hash'}) or policy.get('suite_input_hash')!=suite['input_hash'] or policy.get('holdout_used_for_selection') is not False:
        raise ValueError('policy_identity_mismatch')
    cases=[c for c in suite['cases'] if c['split']=='calibration']
    if policy.get('calibration_case_hash')!=fingerprint(cases):raise ValueError('calibration_inputs_changed')
    if policy!=select_policy(suite):raise ValueError('policy_not_from_locked_calibration')
    report=_evaluate(suite,[c for c in suite['cases'] if c['split']=='holdout'],policy['threshold'],policy['margin'])
    return {'version':VERSION,'suite_input_hash':suite['input_hash'],'policy_hash':policy['policy_hash'],
        'label_hash':suite['label_hash'],'source_identity':suite['source_identity'],'holdout':report,
        'policy_changed':False,'evaluation_mode':'frozen_candidate_policy','provider_calls':0,'formal_policy_written':False}


class ReadOnlyRepository:
    def __init__(self,path):self.path=Path(path).resolve()
    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');yield db


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    freeze=sub.add_parser('freeze');freeze.add_argument('--data-dir',type=Path,required=True);freeze.add_argument('--cases',type=Path,required=True);freeze.add_argument('--provider-signature',required=True);freeze.add_argument('--output',type=Path,required=True)
    for name in ('calibrate','holdout'):
        p=sub.add_parser(name);p.add_argument('--suite',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
        if name=='holdout':p.add_argument('--policy',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.output.exists():parser.error('output_already_exists')
    try:
        if args.command=='freeze':
            from app.intent_management import IntentManagement
            repo=ReadOnlyRepository(args.data_dir/'docqa.db');rows=IntentManagement(repo).list();index=SemanticIntents(repo);data=json.loads(args.cases.read_text('utf-8'))
            result=freeze_suite(rows,index.snapshot_examples(rows,args.provider_signature),data['cases'],args.provider_signature,term_mapping=data.get('term_mapping','{}'),annotation_reference=data.get('annotation_reference'))
        else:
            suite=json.loads(args.suite.read_text('utf-8'));result=select_policy(suite) if args.command=='calibrate' else evaluate_holdout(suite,json.loads(args.policy.read_text('utf-8')))
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    except (ValueError,KeyError,TypeError,EmbeddingError) as exc:parser.error(str(exc) if isinstance(exc,ValueError) else 'invalid_evaluation_input')
    print(json.dumps({'command':args.command,'provider_calls':0,'formal_policy_written':False,'output':str(args.output)}))


if __name__=='__main__':main()
