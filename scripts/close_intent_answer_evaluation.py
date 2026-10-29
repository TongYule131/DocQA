"""N01/N02已封存输入的受控复验；live必须绑定本次80次批准和唯一账本。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack,nullcontext
from copy import deepcopy
from dataclasses import asdict,replace
from hashlib import sha256
import json
from pathlib import Path
import os
import sys
from unittest.mock import patch

import httpx

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.answer_semantic_review import DIMENSIONS,SEMANTIC_REVIEW_VERSION,request as review_request,messages,validate as validate_review,require_accept
from app.config import Settings
from app.deepseek import DeepSeekModel
from app.embedding import APIEmbedding
from app.rag_validation import ModelOutputError,parse_model_output,validate_user_condition_boundary
from scripts.approved_live_calls import ApprovedCalls,MeteredTransport,purpose,hashed
from scripts.evaluate_intent_responsibilities import CaseRuntime,FrozenModel,OfflineEmbedding,digest,new_output,observe,read,write,score
from scripts.evaluate_answer_obligations import restore_writer_pack
from scripts.intent_obligations import field_obligations

FORMAL=Path('D:/python/DocQA')
ORIGINAL_CLIENT=httpx.Client


def accepted_stub(value):return {'version':SEMANTIC_REVIEW_VERSION,'input_hash':value['input_hash'],'verdict':'accept',
    'checks':{d:'pass' for d in DIMENSIONS},'findings':[]}


class ModelSpy:
    def __init__(self,runtime,fixture,provider=None):
        self.runtime,self.fixture,self.provider=runtime,fixture,provider
        self.base=FrozenModel(fixture);self.calls=[];self.outputs=[];self.faults=[]
    @property
    def last_call_metadata(self):return getattr(self.provider,'last_call_metadata',None)
    def generate(self,system,user):
        self.calls.append({'system':system,'user':user});value=json.loads(user)
        name='N01/'+self.runtime.case['id']+'/'+self.runtime.phase+'/generation'
        with purpose(name):
            raw=self.provider.generate(system,user) if self.provider else json.dumps(accepted_stub(value),ensure_ascii=False) if value.get('version')==SEMANTIC_REVIEW_VERSION else self.base.generate(system,user)
        if self.provider and self.fixture['scenario']=='tool_change_known_parameter' and 'schema' in value and value.get('known_arguments'):
            changed=parse_model_output(raw);before=deepcopy(changed)
            # 经批准的故障注入：保留真实响应另存，不能冒充供应商曾改写参数。
            if 'arguments' in changed:changed['arguments']['point']='丙站'
            elif 'point' in changed:changed['point']='丙站'
            else:raise ValueError('fault_injection_protocol_unknown')
            raw=json.dumps(changed,ensure_ascii=False);self.faults.append({'kind':'known_parameter_fault_injection','original':before,'injected':changed})
        self.outputs.append(raw);return raw
    def generate_stream(self,system,user,on_text,check_active):
        if self.provider:
            self.calls.append({'system':system,'user':user})
            with purpose('N01/'+self.runtime.case['id']+'/'+self.runtime.phase+'/generation'):
                raw=self.provider.generate_stream(system,user,on_text,check_active)
            self.outputs.append(raw);return raw
        raw=self.generate(system,user)
        for start in range(0,len(raw),80):check_active();on_text(raw[start:start+80])
        return raw


class SharedEmbedding:
    def __init__(self,runtime,settings,pool,provider=None):
        self.runtime,self.settings,self.pool,self.provider=runtime,settings,pool,provider;self.calls=[];self.query_vector=None
        self.synthetic=OfflineEmbedding(settings)
    @property
    def signature(self):return self.provider.signature if self.provider else self.synthetic.signature
    def embed(self,texts):
        self.calls.append(list(texts))
        if not self.provider:
            self.synthetic.query_vector=self.query_vector
            return self.synthetic.embed(texts)
        if all(text in self.pool for text in texts):return [self.pool[t] for t in texts]
        if len(texts)!=1:raise ValueError('unfrozen_index_text_not_in_preloaded_pool')
        with purpose('N01/'+self.runtime.case['id']+'/'+self.runtime.phase+'/query','query'):
            return self.provider.embed(texts)


class Runtime(CaseRuntime):
    def __init__(self,case,fixture,corpus,settings,directory,pool,provider=None,embedding=None):
        super().__init__(case,fixture,corpus,settings,directory)
        self.live=provider is not None
        self.model=ModelSpy(self,fixture,provider)
        self.embedding=SharedEmbedding(self,settings,pool,embedding)
    def network_guard(self):return nullcontext() if self.live else super().network_guard()


def load_sealed(path):
    manifest=read(path);base=Path(path).parent;labels=read(base/manifest['candidate_file'])
    if (manifest.get('status')!='sealed' or labels.get('sealed') is not True or labels.get('review_status')!='accepted_by_user' or
        sha256((base/manifest['candidate_file']).read_bytes()).hexdigest()!=manifest['candidate_sha256'] or
        sha256((base/manifest['user_authorization_file']).read_bytes()).hexdigest()!=manifest['user_authorization_sha256']):
        raise ValueError('approved_annotation_seal_mismatch')
    return manifest,labels


def freeze(n01_seal,n01_inputs,n02_seal,positive_paths,negative_paths,authorization,output):
    seal1,labels1=load_sealed(n01_seal);seal2,labels2=load_sealed(n02_seal)
    inputs=read(n01_inputs);approval=read(authorization)
    if approval.get('max_model_http')!=80 or approval.get('scope')!='N01-N02-current-run':raise ValueError('current_80_approval_required')
    if len(positive_paths)!=8 or len(negative_paths)!=8:raise ValueError('eight_frozen_pairs_required')
    positives={};negatives={}
    for path in positive_paths:
        value=read(path);positives[value['id']]={'observation':value,'sha256':sha256(Path(path).read_bytes()).hexdigest()}
    for path in negative_paths:
        identifier=Path(path).name.removesuffix('-negative.json');negatives[identifier]=read(path)
    if set(positives)!={c['id'] for c in labels2['cases']} or set(negatives)!=set(positives):raise ValueError('n02_pair_ids_mismatch')
    out=new_output(output)
    from app.runtime import source_identity
    settings=Settings.from_env(FORMAL/'.env')
    public={k:getattr(settings,k) for k in ('deepseek_base_url','deepseek_model','deepseek_thinking','deepseek_reasoning_effort',
        'deepseek_max_tokens','deepseek_timeout_seconds','embedding_base_url','embedding_model','embedding_batch_size','embedding_timeout_seconds')}
    texts=[]
    for split in ('development','calibration','holdout'):
        case=next(c for c in labels1['cases'] if c['split']==split)
        directory=out/('index-preflight-'+split);directory.mkdir()
        isolated=Settings(data_dir=directory,deepseek_api_key='offline-only',embedding_api_key='offline-only',team_mode=True,session_cookie_secure=False)
        with CaseRuntime(case,inputs['cases'][case['id']],inputs['corpora'][split],isolated,directory) as runtime:
            for batch in runtime.embedding.calls:
                for text in batch:
                    if text not in texts:texts.append(text)
    import sqlite3
    ledger=FORMAL/'data/rag-live-v8-20261019/request-ledger.db'
    with sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True) as db:
        start=db.execute('SELECT COALESCE(MAX(sequence),0) FROM calls').fetchone()[0]
        old=list(db.execute('SELECT max_requests,fingerprint FROM approval WHERE id=1').fetchone())
    if start!=approval['used_at_approval']:raise ValueError('ledger_changed_since_grant')
    slots={'N01/index':(len(texts)+settings.embedding_batch_size-1)//settings.embedding_batch_size}
    for case in labels1['cases']:
        identifier=case['id'];calls=case['expected']['calls'];review=int(case['expected']['route_kind']=='KB' and case['expected']['outcome'] in ('answered','partial'))
        slots['N01/'+identifier+'/case/query']=calls['embedding']
        slots['N01/'+identifier+'/case/generation']=calls['generation']+review
        if inputs['cases'][identifier]['scenario'] in ('tool_continue','tool_change_known_parameter','cancelled_confirmation','tool_disabled_confirmation','foreign_confirmation'):
            slots['N01/'+identifier+'/setup/generation']=1
        if identifier=='IR-C12':slots.update({'N01/IR-C12/setup/query':1,'N01/IR-C12/setup/generation':2})
    for case in labels2['cases']:
        identifier=case['id'];slots['N02/'+identifier+'/writer']=1;slots['N02/'+identifier+'/positive_review']=1
        if identifier not in ('N2-R02','N2-R05'):slots['N02/'+identifier+'/negative_review']=1
    if sum(slots.values())>80:raise ValueError('frozen_upper_exceeds_80')
    paths=['scripts/close_intent_answer_evaluation.py','scripts/approved_live_calls.py','scripts/evaluate_intent_responsibilities.py','scripts/evaluate_answer_obligations.py','scripts/intent_obligations.py']
    plan={'stage_id':'n01-n02-closure-20261027-v1','max_model_http':80,'start_sequence':start,'previous_approval':old,
        'http_slots':slots,'planned_http_upper':sum(slots.values()),'allowed_urls':[settings.deepseek_base_url.rstrip('/')+'/chat/completions',settings.embedding_base_url.rstrip('/')+'/embeddings'],
        'settings':public,'n01_labels':labels1,'n01_inputs':inputs,'n02_labels':labels2,'n02_positives':positives,'n02_negatives':negatives,'index_texts':texts,
        'n01_annotation_sha256':seal1['candidate_sha256'],'n02_annotation_sha256':seal2['candidate_sha256'],
        'application_source_identity':source_identity(),'authorization_sha256':sha256(Path(authorization).read_bytes()).hexdigest(),
        'runner_sources':{p:sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
        'stop_conditions':['HTTP超时/不确定/非200立即持久停止，不重发。','模型协议或复核与冻结预期不符则停止本轮，保留首错，不继续消费余量。','源码、模型、输入、授权或账本起点变化不发请求。','C02禁止重发；不改旧行、原件、解析、索引和人工结果。'],
        'old_unused_six_not_added':True,'provider_http_at_freeze':0}
    write(out/'plan.json',plan);(out/'authorization.json').write_bytes(Path(authorization).read_bytes())
    for p in paths:(out/Path(p).name).write_bytes((ROOT/p).read_bytes())
    return {'planned_http_upper':sum(slots.values()),'max_model_http':80,'input_hash':digest(plan),'source_identity':source_identity(),'source_index_texts':len(texts),'settings':public}


def candidate_from_protocol(protocol,pack,question,plan):
    from app.rag_answer_contract import ANSWER_CONTRACT_VERSION,validate_task_output
    from app.rag_composition import validate_plan,validate_writer_plan
    from app.rag_presentation import render_answer
    from app.rag_support_review import review_claims
    from app.rag import RagService
    raw=json.dumps(protocol,ensure_ascii=False)
    if plan:
        validate_plan(plan,pack,question,resolve_label_choices=False);validate_writer_plan(raw,plan,pack,question=question)
    checked=validate_task_output(raw,pack,question=question,contract_version=ANSWER_CONTRACT_VERSION)
    validate_user_condition_boundary(checked,pack,question=question);review_claims(checked,pack)
    return {'status':checked.status,'answer':render_answer(checked),'conclusion':[asdict(f) for f in checked.conclusion],
        'explanation':[asdict(f) for f in checked.explanation],'citations':[c.model_dump() for c in RagService._citations(None,pack,checked.quotes,checked.citation_bindings)],
        'answer_parts':checked.answer_parts,'missing_points':checked.missing_points,'clarification_questions':checked.clarification_questions,'presentation':checked.presentation}


def resume_observation(runtime,source):
    """在新副本复用查询／正文检查点；已知失败只按冻结的一次明确许可重试。"""
    import sqlite3
    from contextlib import closing
    from app.repository import Repository
    from app.knowledge_worker import KnowledgeWorker
    from scripts.evaluate_intent_responsibilities import LocalRuntime
    previous=read(source['observation_file']);task_id=previous['task']['id']
    if sha256(Path(source['observation_file']).read_bytes()).hexdigest()!=source['observation_sha256']:raise ValueError('resume_observation_changed')
    if sha256(Path(source['database_file']).read_bytes()).hexdigest()!=source['database_sha256']:raise ValueError('resume_database_changed')
    with closing(sqlite3.connect(Path(source['database_file']).as_uri()+'?mode=ro',uri=True)) as db,closing(sqlite3.connect(runtime.directory/'docqa.db')) as target:db.backup(target)
    runtime.repository=Repository(runtime.directory/'docqa.db');runtime.repository.settings=runtime.settings
    with runtime.repository.connect() as db:
        protocol=db.execute("SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name='validated_protocol'",(task_id,)).fetchone()
        if source.get('kind')=='known_invalid_planner_explicit_retry':
            if protocol or previous['task']['error_code']!='output_composition_coverage_invalid':raise ValueError('retry_not_a_known_invalid_planner')
            original=db.execute('SELECT * FROM kb_task_calls WHERE task_id=? AND sequence_no=?',(task_id,source['failed_local_sequence'])).fetchone()
            if not original or dict(original)!=source['failed_local_row'] or original['status']!='failed':raise ValueError('original_failed_call_changed')
            # 用户明确允许的一次规划重试；首次已收费失败原行保持，不退预算。
            db.execute("UPDATE kb_tasks SET status='queued',error_code=NULL,error_message=NULL,finished_at=NULL WHERE id=?",(task_id,))
        else:
            if not protocol or json.loads(protocol[0])!=parse_model_output(previous['model_outputs'][0]):raise ValueError('resume_draft_mismatch')
            pending=db.execute("SELECT * FROM kb_task_calls WHERE task_id=? AND sequence_no=?",(task_id,source['not_sent_local_sequence'])).fetchone()
            if not pending or dict(pending)!=source['not_sent_local_row']:raise ValueError('resume_not_sent_tombstone_changed')
            # 只改新副本；原失败DB／行／观察不动。唯一HTTP账本证明此本地预扣未发送。
            db.execute("UPDATE kb_task_calls SET status='failed',delivery_status='not_sent',error_code='not_sent_verified_by_unique_http_ledger' WHERE id=?",(pending['id'],))
            db.execute("UPDATE kb_tasks SET status='queued',requests_used=requests_used-1,error_code=NULL,error_message=NULL,finished_at=NULL WHERE id=?",(task_id,))
    runtime.phase='case';runtime.model.calls=previous['model_inputs'][:len(previous['model_outputs'])];runtime.model.outputs=list(previous['model_outputs'])
    worker=KnowledgeWorker(runtime.settings,runtime.repository,LocalRuntime(),embedding=runtime.embedding,model=runtime.model)
    worker.run_once(task_id);row=worker.tasks.row(task_id);result=json.loads(row['result_json']) if row['result_json'] else None
    observed=deepcopy(previous);observed.update(task=row,result=result,presented_result=result,
        actual={'route_kind':'KB','action':previous['payload']['execution']['action'],'outcome':result['status'] if result else row['status']},
        main_calls={**previous['main_calls'],'generation':len(runtime.model.outputs)},model_inputs=runtime.model.calls,model_outputs=runtime.model.outputs)
    with runtime.repository.connect() as db:observed['calls']=[dict(c) for c in db.execute('SELECT * FROM kb_task_calls WHERE task_id=? ORDER BY sequence_no',(task_id,))]
    observed['new_call_rows']=len(observed['calls'])
    observed['resume_evidence']={'source_observation_sha256':source['observation_sha256'],'writer_reused':source.get('kind')!='known_invalid_planner_explicit_retry',
        'query_reused':True,'new_provider_calls':len(runtime.model.outputs)-len(previous['model_outputs'])}
    return observed


def evaluate_n02_pair(plan,case,directory,live,model,budget=None):
    """正向生成与封存负例分别执行，不重试；明确的题目失败可按冻结计划独立继续。"""
    identifier=case['id'];saved=plan['n02_positives'][identifier]['observation'];pack,writer=restore_writer_pack(case,saved)
    row={'id':identifier,'positive_review':None,'positive_error':None,'negative_structural_reason':None,
        'negative_semantic_verdict':None,'negative_error':None,'engineering_passed':False,'real_provider':live}
    def failure(stage,exc):
        if not plan.get('independent_n02_cases') or budget is not None and budget.stopped:raise exc
        import traceback
        (directory/(stage+'-first-failure.txt')).write_text(traceback.format_exc(),'utf-8')
        row[stage+'_error']=getattr(exc,'reason',str(exc))
    try:
        system,user=saved['model_inputs'][-1]
        if live:
            with purpose('N02/'+identifier+'/writer'):raw=model.generate(system,user)
        else:raw=saved['model_outputs'][-1]
        (directory/'write-response.txt').write_text(raw,'utf-8')
        candidate=candidate_from_protocol(parse_model_output(raw),pack,case['question'],writer.get('composition_plan'))
        if candidate['status']!=saved['result']['status']:raise ValueError('n02_status_changed_'+identifier)
        value=review_request(case['question'],pack,candidate);rs,ru=messages(value);write(directory/'positive-review-request.json',value)
        if live:
            with purpose('N02/'+identifier+'/positive_review'):rr=model.generate(rs,ru)
        else:rr=json.dumps(accepted_stub(value),ensure_ascii=False)
        (directory/'positive-review-response.txt').write_text(rr,'utf-8')
        report=validate_review(rr,value);write(directory/'positive-review.json',report);row['positive_review']=report['verdict'];require_accept(report)
    except (ModelOutputError,ValueError) as exc:failure('positive',exc)
    try:
        negative=plan['n02_negatives'][identifier]
        try:bad=candidate_from_protocol(negative['response'],pack,case['question'],negative['plan'])
        except ModelOutputError as exc:row['negative_structural_reason']=exc.reason
        if row['negative_structural_reason'] is None:
            if not plan['http_slots'].get('N02/'+identifier+'/negative_review'):raise ValueError('structural_negative_guard_failed_'+identifier)
            value=review_request(case['question'],pack,bad);rs,ru=messages(value);write(directory/'negative-review-request.json',value)
            if live:
                with purpose('N02/'+identifier+'/negative_review'):raw=model.generate(rs,ru)
            else:
                response=accepted_stub(value);response['verdict']='reject';response['checks']['factual_support']='fail';source=value['source_items'][0]
                response['findings']=[{'dimensions':['factual_support'],'fact_indices':[0],'part_ids':[value['answer']['answer_parts'][0]['part_id']],
                    'sources':[{'source_id':source['source_id'],'quote':source['ranges'][0]['text']}],'explanation':'离线接口桩，不认证语义。'}]
                raw=json.dumps(response,ensure_ascii=False)
            (directory/'negative-review-response.txt').write_text(raw,'utf-8')
            report=validate_review(raw,value);write(directory/'negative-review.json',report);row['negative_semantic_verdict']=report['verdict']
            if report['verdict']!='reject':raise ValueError('negative_not_rejected_'+identifier)
    except (ModelOutputError,ValueError) as exc:failure('negative',exc)
    row['engineering_passed']=row['positive_review']=='accept' and row['positive_error'] is None and row['negative_error'] is None and bool(
        row['negative_structural_reason'] or row['negative_semantic_verdict']=='reject')
    return row


def run(plan_path,output,live=False):
    plan=read(plan_path);base=Path(plan_path).parent;approval=read(base/'authorization.json')
    from app.runtime import source_identity
    if source_identity()!=plan['application_source_identity']:raise ValueError('application_changed_refreeze_required')
    if sha256((base/'authorization.json').read_bytes()).hexdigest()!=plan['authorization_sha256']:raise ValueError('authorization_changed')
    if plan.get('explicit_retry_generation_allowance'):
        allowed=plan.get('explicit_retry_authorization',{});content=Path(allowed.get('file','')).read_bytes()
        if sha256(content).hexdigest()!=allowed.get('sha256') or read(allowed['file']).get('approved_once') is not True:
            raise ValueError('explicit_retry_authorization_missing_or_changed')
    for p,h in plan['runner_sources'].items():
        if sha256((ROOT/p).read_bytes()).hexdigest()!=h:raise ValueError('runner_changed_refreeze_required')
    out=new_output(output);model=embedding=budget=None;pool={};reports=[];stack=ExitStack()
    try:
        if live:
            settings=Settings.from_env(FORMAL/'.env')
            if any(getattr(settings,k)!=v for k,v in plan['settings'].items()):raise ValueError('provider_settings_changed')
            if not settings.deepseek_api_key or not settings.embedding_api_key:raise ValueError('provider_keys_missing')
            settings=replace(settings,data_dir=out,team_mode=True,session_cookie_secure=False,answer_semantic_review_enabled=True)
            budget=ApprovedCalls(FORMAL/'data/rag-live-v8-20261019/request-ledger.db',plan,approval,out/'http')
            import app.deepseek as dm,app.embedding as em
            original=dm.OpenAI
            def sdk(**kwargs):
                kwargs['http_client']=ORIGINAL_CLIENT(transport=MeteredTransport(budget,httpx.HTTPTransport(retries=0)),timeout=kwargs['timeout'],follow_redirects=False)
                return original(**kwargs)
            stack.enter_context(patch.object(dm,'OpenAI',sdk));stack.enter_context(patch.object(em,'OpenAI',sdk))
            model=DeepSeekModel(settings);embedding=APIEmbedding(settings)
            budget.activate()
            if plan.get('index_cache'):
                cache=plan['index_cache'];content=Path(cache['file']).read_bytes()
                if sha256(content).hexdigest()!=cache['sha256']:raise ValueError('index_cache_changed')
                value=json.loads(content)
                if value['texts']!=plan['index_texts'] or value['model_signature']!=embedding.signature:raise ValueError('index_cache_identity_mismatch')
                vectors=value['vectors']
            else:
                with purpose('N01/index','index'):vectors=embedding.embed(plan['index_texts'])
            pool=dict(zip(plan['index_texts'],vectors))
            write(out/'index-cache.json',{'model_signature':embedding.signature,'texts':plan['index_texts'],'vectors':vectors})
        else:settings=Settings(data_dir=out,deepseek_api_key='offline-only',embedding_api_key='offline-only',team_mode=True,session_cookie_secure=False)
        for case in plan['n01_labels']['cases']:
            identifier=case['id'];fixture=plan['n01_inputs']['cases'][identifier];directory=out/identifier;directory.mkdir()
            if identifier in plan.get('retained_failed_cases',{}):
                saved=plan['retained_failed_cases'][identifier];content=Path(saved['report_file']).read_bytes()
                if sha256(content).hexdigest()!=saved['sha256']:raise ValueError('retained_failure_report_changed')
                prior=json.loads(content);result=deepcopy(next(r for r in prior['cases'] if r['id']==identifier))
                if result['engineering_passed'] or prior['source_identity']!=plan['application_source_identity']:raise ValueError('retained_failure_identity_invalid')
                result['retained_failure']=True;reports.append(result);continue
            if identifier in plan.get('completed_cases',{}):
                saved=plan['completed_cases'][identifier];content=Path(saved['report_file']).read_bytes()
                if sha256(content).hexdigest()!=saved['sha256']:raise ValueError('completed_case_report_changed')
                prior=json.loads(content);result=next(r for r in prior['cases'] if r['id']==identifier)
                if not result['engineering_passed'] or prior['source_identity']!=plan['application_source_identity']:raise ValueError('completed_case_not_reusable')
                reports.append(result);continue
            config=replace(settings,data_dir=directory,question_max_requests=8,answer_semantic_review_enabled=True,**fixture.get('settings',{}))
            runtime=Runtime(case,fixture,plan['n01_inputs']['corpora'][case['split']],config,directory,pool,model,embedding)
            try:
                with (nullcontext(runtime) if identifier in plan.get('resume_cases',{}) else runtime):
                    observed=resume_observation(runtime,plan['resume_cases'][identifier]) if identifier in plan.get('resume_cases',{}) else observe(runtime)
                    observed['fault_injections']=runtime.model.faults
                    write(directory/'observed.json',observed)
                    profile=deepcopy(case)
                    if case['expected']['route_kind']=='KB' and case['expected']['outcome'] in ('answered','partial'):
                        profile['expected']['calls']['generation']+=1
                        if profile['expected'].get('calls_by_phase'):
                            profile['expected']['calls_by_phase']['initial_submission']['generation']+=1
                    profile['expected']['calls']['generation']+=plan.get('explicit_retry_generation_allowance',{}).get(identifier,0)
                    result=score(profile,observed);fields=field_obligations(case,observed,fixture)
                    # 本profile显式增加一次只读复核；封存旧上界保持，不覆盖旧报告。
                    if case['id']=='IR-D12':
                        for check in fields['checks']:
                            if check['obligation']=='same_task_submission_replayed':
                                check['status']='passed' if observed['new_tasks']==1 and observed['new_call_rows']==3 else 'failed'
                        fields['complete']=all(c['status']=='passed' for c in fields['checks'])
                    result['field_obligations']=fields
                    result['engineering_passed'] &= fields['complete']
                    result['real_provider']=live;reports.append(result)
            finally:runtime.stack.close()
            print(json.dumps({'id':identifier,'passed':result['engineering_passed'],'actual':result['actual']},ensure_ascii=False),flush=True)
            if not result['engineering_passed']:raise ValueError('n01_unexpected_result_'+identifier)
        for case in plan['n02_labels']['cases']:
            identifier=case['id'];directory=out/identifier;directory.mkdir()
            if identifier in plan.get('retained_failed_cases',{}):
                saved=plan['retained_failed_cases'][identifier];content=Path(saved['report_file']).read_bytes()
                if sha256(content).hexdigest()!=saved['sha256']:raise ValueError('retained_failure_report_changed')
                prior=json.loads(content);row=deepcopy(next(r for r in prior['cases'] if r['id']==identifier))
                if row['engineering_passed'] or prior['source_identity']!=plan['application_source_identity']:raise ValueError('retained_failure_identity_invalid')
                row['retained_failure']=True
            else:row=evaluate_n02_pair(plan,case,directory,live,model,budget)
            reports.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
            if not row['engineering_passed'] and not plan.get('independent_n02_cases'):raise ValueError('n02_unexpected_result_'+identifier)
        if source_identity()!=plan['application_source_identity']:raise ValueError('application_changed_during_run')
    except Exception as exc:
        if budget:budget.stop('stopped_unexpected_result')
        import traceback
        (out/'first-failure.txt').write_text(traceback.format_exc(),'utf-8')
        summary={'passed':False,'exception':type(exc).__name__,'message':str(exc),'completed_cases':len(reports),'cases':reports,
            'source_identity':source_identity(),'plan_sha256':digest(plan),'real_provider':live,'model_http':budget.report() if budget else {'used':0},'local_full_executed':False}
        write(out/'report.json',summary);raise
    finally:stack.close()
    passed=bool(reports) and all(r['engineering_passed'] for r in reports)
    if budget:budget.stop('complete' if passed else 'complete_with_failed_cases')
    result={'passed':passed,'cases':reports,'completed_cases':len(reports),'passed_cases':sum(r['engineering_passed'] for r in reports),
        'retained_failed_cases':[r['id'] for r in reports if r.get('retained_failure')],'source_identity':source_identity(),'plan_sha256':digest(plan),
        'real_provider':live,'model_http':budget.report() if budget else {'used':0},'local_full_executed':False,
        'independent_blind_evaluation':False,'human_acceptance_replaced':False}
    write(out/'report.json',result)
    return {k:v for k,v in result.items() if k!='cases'}


def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='command',required=True)
    f=s.add_parser('freeze')
    for key in ('n01-seal','n01-inputs','n02-seal','authorization'):f.add_argument('--'+key,type=Path,required=True)
    f.add_argument('--positive',type=Path,nargs='+',required=True);f.add_argument('--negative',type=Path,nargs='+',required=True);f.add_argument('--output',type=Path,required=True)
    r=s.add_parser('run');r.add_argument('--plan',type=Path,required=True);r.add_argument('--output',type=Path,required=True);r.add_argument('--live',action='store_true')
    args=p.parse_args();os.environ['DOCQA_DATA_DIR']=str(args.output.resolve())
    result=(freeze(args.n01_seal,args.n01_inputs,args.n02_seal,args.positive,args.negative,args.authorization,args.output) if args.command=='freeze' else run(args.plan,args.output,args.live))
    print(json.dumps(result,ensure_ascii=False))
    if args.command=='run' and result['passed'] is not True:raise SystemExit(1)


if __name__=='__main__':main()
