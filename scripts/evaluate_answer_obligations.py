"""封存长答案职责的隔离入口／worker核对；模型为冻结响应桩。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.evaluate_intent_responsibilities import CaseRuntime,FrozenModel,digest,new_output,no_external_network,observe,read,write


class ObligationModel(FrozenModel):
    def generate(self,system,user):
        self.calls.append((system,user));value=json.loads(user)
        if 'evidence_units' in value:
            catalog=[{'ref':u['ref'],'unit':u['unit'],'text':''.join(p['text'] for p in u['passages'])} for u in value['evidence_units']]
            blocks=[]
            for spec in self.fixture['plan_blocks']:
                groups=[]
                for quotes in spec['groups']:
                    units=self.select_units(catalog,quotes)
                    source=next(u for u in value['evidence_units'] if (u['ref'],u['unit'])==(units[0]['ref'],units[0]['unit']))
                    group={'label':'原文依据'+str(len(groups)+1),'units':units}
                    if source.get('label_choices'):
                        group.update(label=next(iter(source['label_choices'])),label_source=units[0])
                    groups.append(group)
                blocks.append({'role':spec.get('role') or value['organization_brief']['first_role'],
                    'focus':'核对所问条件与关系','part_ids':spec['part_ids'],'groups':groups})
            result={'blocks':blocks,'missing_part_ids':self.fixture['missing_part_ids']}
        else:
            def unit_text(reference,parts):
                texts=[]
                for part in parts:
                    ref,span,first,last=part if len(part)==4 else (reference['ref'],*part)
                    source=next(r for r in value['references'] if r['ref']==ref)
                    window=next(w for w in source['content'] if w['span']==span)
                    texts.extend(window['segments'][str(i)] for i in range(first,last+1))
                return ''.join(texts)
            catalog=[{'ref':r['ref'],'unit':int(k),'text':unit_text(r,parts)} for r in value['references'] for k,parts in r['quote_units'].items()]
            facts=[{'text':f['text'],'part_ids':f['part_ids'],'claim_type':f['claim_type'],'context':[],
                    'evidence':self.select_units(catalog,f['quote_texts'])} for f in self.fixture['facts']]
            plan=value.get('composition_plan')
            blocks=[{'role':b['role'],'kind':'paragraph','fact_indices':indices} for b,indices in zip(plan['blocks'],self.fixture['block_facts'])] if plan else [
                {'role':'evidence','kind':'paragraph','fact_indices':list(range(len(facts)))}]
            parts=self.fixture['answer_parts'];gaps=[p['missing_detail'] for p in parts if p['state']=='missing_evidence']
            result={'status':'partial' if gaps else 'answered','conclusion':facts,'explanation':[],
                'clarification_questions':[],'missing_points':gaps,'answer_parts':parts,'presentation':{'blocks':blocks}}
        raw=json.dumps(result,ensure_ascii=False);self.outputs.append(raw);return raw


class ObligationRuntime(CaseRuntime):
    def __init__(self,*args):
        super().__init__(*args);self.model=ObligationModel(self.fixture)


def freeze(seal_path,inputs_path,output):
    seal=read(seal_path);base=Path(seal_path).parent;candidate=base/seal['candidate_file'];labels=read(candidate)
    if seal.get('status')!='sealed' or labels.get('sealed') is not True or labels.get('review_status')!='accepted_by_user':
        raise ValueError('human_seal_required')
    if sha256(candidate.read_bytes()).hexdigest()!=seal['candidate_sha256'] or sha256((base/seal['user_authorization_file']).read_bytes()).hexdigest()!=seal['user_authorization_sha256']:
        raise ValueError('seal_or_authorization_changed')
    inputs=read(inputs_path)
    if set(inputs['cases'])!={c['id'] for c in labels['cases']}:raise ValueError('input_case_identity_mismatch')
    for case in labels['cases']:
        for source in case['sources']:
            content=(base/'sources'/(source['id']+'.txt')).read_bytes()
            if content!=source['text'].encode('utf-8') or sha256(content).hexdigest()!=seal['source_sha256'][source['id']+'.txt']:
                raise ValueError('approved_source_changed')
    out=new_output(output)
    from app.runtime import source_identity
    paths=['scripts/evaluate_answer_obligations.py','scripts/evaluate_intent_responsibilities.py']
    bundle={'version':'answer-obligations-runtime-v1','labels':labels,'inputs':inputs,
        'seal_sha256':sha256(Path(seal_path).read_bytes()).hexdigest(),'annotation_sha256':seal['candidate_sha256'],
        'runner_sources':{p:sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
        'application_source_identity':source_identity(),'provider_http':0}
    bundle['input_hash']=digest({k:v for k,v in bundle.items() if k not in ('application_source_identity',)})
    write(out/'bundle.json',bundle);(out/'input-fixtures.json').write_bytes(Path(inputs_path).read_bytes())
    for p in paths:(out/Path(p).name).write_bytes((ROOT/p).read_bytes())
    sources=out/'sources';sources.mkdir()
    for case in labels['cases']:
        for source in case['sources']:(sources/(source['id']+'.txt')).write_bytes(source['text'].encode('utf-8'))
    return {'input_hash':bundle['input_hash'],'total':len(labels['cases']),'provider_http':0}


def run(bundle_path,case_ids,output):
    bundle=read(bundle_path)
    if digest({k:v for k,v in bundle.items() if k not in ('application_source_identity','input_hash')})!=bundle['input_hash']:
        raise ValueError('input_bundle_changed')
    from app.runtime import source_identity
    from app.config import Settings
    if source_identity()!=bundle['application_source_identity']:raise ValueError('application_changed_refreeze_required')
    for p,h in bundle['runner_sources'].items():
        if sha256((ROOT/p).read_bytes()).hexdigest()!=h:raise ValueError('runner_changed_refreeze_required')
    cases=bundle['labels']['cases']
    if case_ids:
        if len(set(case_ids))!=len(case_ids) or set(case_ids)-{c['id'] for c in cases}:raise ValueError('invalid_case_selection')
        cases=[c for c in cases if c['id'] in case_ids]
    out=new_output(output);reports=[]
    for approved in cases:
        identifier=approved['id'];directory=out/identifier;directory.mkdir()
        case={**approved,'split':identifier.lower()};fixture=bundle['inputs']['cases'][identifier]
        corpus=[{'key':s['id'],'filename':s['id']+'.txt','paragraphs':[s['text']]} for s in approved['sources']]
        upper=approved['offline_stub_call_upper_bound']
        settings=Settings(data_dir=directory,deepseek_api_key='offline-only',embedding_api_key='offline-only',
            semantic_intent_enabled=False,query_planner_enabled=False,memory_summary_enabled=False,reranker_replay_path='',
            question_max_requests=upper['embedding']+upper['generation'],team_mode=True,session_cookie_secure=False)
        runtime=ObligationRuntime(case,fixture,corpus,settings,directory)
        try:
            with runtime:
                observed=observe(runtime);write(directory/'observed.json',observed)
                result=observed.get('result') or {};parts=result.get('answer_parts',[])
                expected=fixture['answer_parts']
                dimensions={'action':observed['actual']['action']=='retrieve',
                    'status':result.get('status')==('partial' if fixture['missing_part_ids'] else 'answered'),
                    'question_parts':[(p['part_id'],p['state']) for p in parts]==[(p['part_id'],p['state']) for p in expected],
                    'calls':observed['main_calls']==upper,
                    'citation_scope':bool(result.get('citations')) and all(c['document_id'] in observed['expected_document_ids'] for c in result['citations'])}
                report={'id':identifier,'dimensions':dimensions,'engineering_passed':all(dimensions.values()),
                    'actual':observed['actual'],'calls':observed['main_calls'],'entry_http_calls':observed['entry_http_calls']}
        except Exception as exc:
            runtime.stack.close()
            import traceback
            (directory/'harness-error.txt').write_text(traceback.format_exc(),'utf-8')
            report={'id':identifier,'engineering_passed':False,'harness_error':type(exc).__name__}
        report.update(semantic_correctness='not_evaluated_stubbed_provider',language_quality='not_evaluated_stubbed_provider')
        reports.append(report);print(json.dumps(report,ensure_ascii=False),flush=True)
    report={'version':'answer-obligations-runtime-v1','input_hash':bundle['input_hash'],'annotation_sha256':bundle['annotation_sha256'],
        'application_source_identity':source_identity(),'runner_sources':bundle['runner_sources'],'cases':reports,
        'engineering_passed':sum(r['engineering_passed'] for r in reports),'total':len(reports),
        'explicit_case_ids':case_ids,'provider_http':0,'full_language_acceptance_passed':False,'local_full_executed':False}
    write(out/'report.json',report)
    return {k:v for k,v in report.items() if k!='cases'}


def restore_writer_pack(case, observed):
    """用实际公布的窗口重建只读校验输入；原文隐藏区不能进入引用选项。"""
    from app.rag_context import EvidenceItem,EvidencePack
    from app.rag_extract_context import strict_segments,merge_ranges
    from app.quote_spans import quote_choice_ranges
    request=json.loads(observed['model_inputs'][-1][1]);items=[]
    if observed['question']!=case['question'] or request['question']!=case['question']:
        raise ValueError('observed_question_identity_changed')
    sources={s['id']+'.txt':s['text'] for s in case['sources']}
    for reference in request['references']:
        text=sources[reference['document_name']];spans=quote_choice_ranges(text);visible=[]
        for window in reference['content']:
            start,end=spans[window['span']-1];segments=strict_segments(text[start:end])
            for number,value in window['segments'].items():
                number=int(number)
                if number<1 or number>len(segments) or segments[number-1]!=value:raise ValueError('visible_source_changed')
                a=start+sum(map(len,segments[:number-1]));visible.append([a,a+len(value)])
        ref=reference['ref']
        choices={key:[p if len(p)==4 else [ref,*p] for p in values] for key,values in reference['quote_units'].items()}
        items.append(EvidenceItem(reference_id=ref,document_id=case['id'],parse_version_id='approved-synthetic',
            chunk_id=reference['document_name'],text=text,page=None,read_ranges=merge_ranges(visible),
            segment_policy='sentence-segments-v2',unit_choices=choices))
    return EvidencePack(items=items,candidates=[],index_id='saved-visible-input',parse_version_id='approved-synthetic',
        truncated=False,context_chars=sum(len(i.text) for i in items)),request


def probe(seal_path,observed_paths,output):
    seal=read(seal_path);base=Path(seal_path).parent;candidate=base/seal['candidate_file'];labels=read(candidate)
    if seal.get('status')!='sealed' or sha256(candidate.read_bytes()).hexdigest()!=seal['candidate_sha256']:
        raise ValueError('annotation_seal_changed')
    if sha256((base/seal['user_authorization_file']).read_bytes()).hexdigest()!=seal['user_authorization_sha256']:
        raise ValueError('annotation_authorization_changed')
    cases={c['id']:c for c in labels['cases']};out=new_output(output);rows=[];bound={};seen=set()
    from app.rag_answer_contract import ANSWER_CONTRACT_VERSION,validate_task_output
    from app.rag_composition import validate_plan,validate_writer_plan
    from app.rag_support_review import review_claims
    from app.rag_validation import ModelOutputError,validate_user_condition_boundary
    from app.runtime import source_identity
    for path in observed_paths:
        o=read(path);identifier=o['id'];case=cases[identifier]
        if identifier in seen:raise ValueError('duplicate_observation')
        seen.add(identifier);bound[str(path)]=sha256(Path(path).read_bytes()).hexdigest()
        pack,request=restore_writer_pack(case,o);original=json.loads(o['model_outputs'][-1]);plan=request.get('composition_plan')
        def validate(body,planning):
            raw=json.dumps(body,ensure_ascii=False)
            if planning:
                validate_plan(planning,pack,case['question'],resolve_label_choices=False)
                validate_writer_plan(raw,planning,pack,question=case['question'])
            checked=validate_task_output(raw,pack,question=case['question'],contract_version=ANSWER_CONTRACT_VERSION)
            validate_user_condition_boundary(checked,pack,question=case['question']);review_claims(checked,pack)
        with no_external_network():validate(original,plan)
        negative=deepcopy(original);changed_plan=deepcopy(plan)
        if identifier=='N2-R01':negative['conclusion'][0]['text']='设备借用应在本次作业结束后归还。'
        if identifier=='N2-R02':negative['conclusion'][0]['part_ids']=['p1']
        if identifier=='N2-R03':
            negative['conclusion'][0]['text']+='夜间也采用相同受理规则。'
            negative['conclusion'][0]['part_ids']=['p1','p2'];negative['answer_parts'][1].update(state='answered',missing_detail='')
            negative.update(status='answered',missing_points=[])
            changed_plan['blocks'][0]['part_ids']=['p1','p2'];changed_plan['missing_part_ids']=[]
        if identifier=='N2-R04':negative['conclusion'][0]['text']='临时领用在离岗时交还；岗位配发在任务结束后交还。'
        if identifier=='N2-R05':changed_plan['blocks'][0]['groups'][0]['label_source']=deepcopy(changed_plan['blocks'][0]['groups'][1]['label_source'])
        if identifier=='N2-R06':
            negative['conclusion'][0]['text']='甲类工具仅供室内使用。'
            repeated=deepcopy(negative['conclusion'][0]);repeated['text']='甲类工具的使用范围限于室内。'
            negative['conclusion'].append(repeated);negative['presentation']['blocks'][0]['fact_indices']=[0,1]
        if identifier=='N2-R07':negative['conclusion'][0]['text']='先登记再领取证明登记使故障率下降。'
        if identifier=='N2-R08':negative['conclusion'][0]['text']='乙规程更新，应按乙规程的启用条件执行。'
        reason=None
        try:
            with no_external_network():validate(negative,changed_plan)
        except ModelOutputError as exc:reason=exc.reason
        write(out/(identifier+'-negative.json'),{'response':negative,'plan':changed_plan})
        rows.append({'id':identifier,'approved_negative_rubric':case['negative_variant'],
            'positive_guard_replay_passed':True,'automatic_rejection':reason is not None,'reason':reason,
            'manual_semantic_gate_required':reason is None,'semantic_quality_score':None})
    result={'version':'answer-obligation-negative-probes-v1','annotation_sha256':seal['candidate_sha256'],
        'source_identity':source_identity(),'input_artifact_sha256':bound,'cases':rows,'total':len(rows),
        'automatic_rejections':sum(r['automatic_rejection'] for r in rows),
        'manual_semantic_gate_required':sum(r['manual_semantic_gate_required'] for r in rows),
        'full_language_acceptance_passed':False,'provider_http':0,'capture_rerun':False,'local_full_executed':False}
    write(out/'report.json',result);(out/'probe-runner.py').write_bytes(Path(__file__).read_bytes())
    return {k:v for k,v in result.items() if k not in ('cases','input_artifact_sha256')}


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    f=sub.add_parser('freeze');f.add_argument('--seal',type=Path,required=True);f.add_argument('--inputs',type=Path,required=True);f.add_argument('--output',type=Path,required=True)
    r=sub.add_parser('run');r.add_argument('--bundle',type=Path,required=True);r.add_argument('--case-id',action='append');r.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('probe');a.add_argument('--seal',type=Path,required=True);a.add_argument('--observed',type=Path,nargs='+',required=True);a.add_argument('--output',type=Path,required=True)
    args=p.parse_args();os.environ['DOCQA_DATA_DIR']=str(args.output.resolve())
    result=(freeze(args.seal,args.inputs,args.output) if args.command=='freeze' else run(args.bundle,args.case_id,args.output) if args.command=='run' else
        probe(args.seal,args.observed,args.output))
    print(json.dumps(result,ensure_ascii=False))
    return 0 if 'engineering_passed' not in result or result['total']>0 and result['engineering_passed']==result['total'] else 1


if __name__=='__main__':raise SystemExit(main())
