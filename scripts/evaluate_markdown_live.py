"""N03封存后的逐步真实验收：唯一账本预扣；每题语义复核后才允许下一步。"""
from __future__ import annotations
import argparse
from contextlib import ExitStack,closing
from dataclasses import replace
from hashlib import sha256
import importlib,json,os,shutil,sqlite3,sys
from pathlib import Path
from unittest.mock import patch
import httpx

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
FORMAL=Path('D:/python/DocQA');LEDGER=FORMAL/'data/rag-live-v8-20261019/request-ledger.db'
from app.config import Settings
from app.runtime import source_identity,code_fingerprint
from app.embedding import APIEmbedding
from app.deepseek import DeepSeekModel
from scripts.approved_live_calls import ApprovedCalls,MeteredTransport,purpose,hashed
from scripts.evaluate_markdown_lifecycle import Runtime as OfflineRuntime,LocalRuntime,retention

def h(p):return sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text('utf-8'))
def save(p,v):
    with Path(p).open('x',encoding='utf-8') as f:json.dump(v,f,ensure_ascii=False,indent=2)
def rows_hash(db):return hashed([list(r) for r in db.execute('SELECT * FROM calls ORDER BY sequence')])

def load_labels(seal):
    seal=Path(seal);manifest=read(seal);folder=seal.parent
    labels=read(folder/manifest['candidate_file'])
    if (manifest.get('status')!='sealed' or labels.get('sealed') is not True or labels.get('review_status')!='accepted_by_user' or
        h(folder/manifest['candidate_file'])!=manifest['candidate_sha256'] or h(folder/manifest['user_authorization_file'])!=manifest['user_authorization_sha256']):
        raise ValueError('n03_annotation_seal_required')
    return labels

def freeze(review,seal,output):
    review=Path(review).resolve();seal=Path(seal).resolve();labels=load_labels(seal);draft=read(review/'plan-candidate.json')
    assert labels['source_sha256']==h(review/'source.md')==draft['source_sha256']
    assert read(review/'candidate.json')['cases']==labels['cases'] and len(labels['cases'])==7
    assert source_identity()==draft['application_source_identity']
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    original=Path(draft['old_baseline']);old=read(original/'report.json')
    assert old['passed'] and h(original/'report.json')==draft['old_report_sha256']
    assert h(draft['new_reference_report'])==draft['new_report_sha256']
    for source in [original/'source.md',original/'report.json',seal,seal.parent/'candidate.json',seal.parent/'user-authorization.json',review.parent/'authorization.json']:
        target=output/({'source.md':'source.md','report.json':'old-report.json','sealed-manifest.json':'sealed-manifest.json',
            'candidate.json':'labels.json','user-authorization.json':'label-authorization.json','authorization.json':'authorization.json'}[source.name])
        target.write_bytes(source.read_bytes())
    runtime=output/'runtime';runtime.mkdir()
    origin_runtime=Path(draft['reuse_runtime_from']) if draft.get('reuse_runtime_from') else original
    with closing(sqlite3.connect((origin_runtime/'docqa.db').as_uri()+'?mode=ro',uri=True)) as db,closing(sqlite3.connect(runtime/'docqa.db')) as target:db.backup(target)
    for folder in ['uploads','parse-results']:shutil.copytree(origin_runtime/folder,runtime/folder)
    (runtime/'source.md').write_bytes((review/'source.md').read_bytes())
    fixture_setup=None
    if draft.get('isolated_fixture_provider_binding'):
        # 仅合成库夹具的初始声明；不声称产品支持原知识库换模型或正式迁移。
        settings=Settings.from_env(FORMAL/'.env');signature=APIEmbedding(settings).signature
        with closing(sqlite3.connect(runtime/'docqa.db')) as db,db:
            before=list(db.execute('SELECT embedding_signature,embedding_model FROM knowledge_bases WHERE id=?',(old['base_id'],)).fetchone())
            db.execute('UPDATE knowledge_bases SET embedding_signature=?,embedding_model=? WHERE id=?',(signature,settings.embedding_model,old['base_id']))
        fixture_setup={'isolated_synthetic_base_only':True,'base_id':old['base_id'],'old_declaration':before,
            'new_declaration':[signature,settings.embedding_model],'old_rows_and_original_database_not_rewritten':True,
            'not_a_product_model_migration_test':True}
    with closing(sqlite3.connect(LEDGER.as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==draft.get('activation_sequence',draft['start_sequence'])
        assert rows_hash(db)==draft['old_ledger_rows_sha256']
        assert list(db.execute('SELECT max_requests,fingerprint FROM approval WHERE id=1').fetchone())==draft['previous_approval']
        assert db.execute("SELECT COUNT(*) FROM evaluation_stage_approvals WHERE status='active'").fetchone()[0]==0
        with closing(sqlite3.connect(output/'ledger-before-live.db')) as target:db.backup(target)
    for name in ['request-ledger.json','approval.json']:(output/('before-'+name)).write_bytes((LEDGER.parent/name).read_bytes())
    old_texts=list(dict.fromkeys(c['text'] for c in old['chunks']));new=read(draft['new_reference_report'])
    steps=['index-old']+['ask-old-'+c['id'] for c in labels['cases'] if c['phase']=='old_and_new']+['parse-new','index-new']+['ask-new-'+c['id'] for c in labels['cases']]+['verify']
    sources=['scripts/evaluate_markdown_live.py','scripts/evaluate_markdown_lifecycle.py','scripts/approved_live_calls.py']
    retained=draft.get('retained_steps',{})
    for step,proof in retained.items():
        source=Path(proof['result_file']);assert h(source)==proof['sha256']
        row=read(source)
        assert row['engineering_passed'] or (draft.get('independent_case_failures') and step.startswith('ask-') and row.get('task',{}).get('error_code','').startswith('output_'))
        target=output/'steps'/step;target.mkdir(parents=True);(target/'result.json').write_bytes(source.read_bytes())
    plan={**draft,'annotation_state':'sealed','labels_sha256':h(output/'labels.json'),'seal_sha256':h(seal),
        'authorization_sha256':h(output/'authorization.json'),'steps':steps,'fixture_setup':fixture_setup,'index_texts':{'old':old_texts,'new':list(dict.fromkeys(c['text'] for c in new['chunks']))},
        'activate_on_step':next(step for step in steps if step not in retained),
        'query_texts':{c['id']:c['question']+'\n用户会话上下文（不是证据）：\n\n' for c in labels['cases']},
        'runner_sources':{p:h(ROOT/p) for p in sources},'input_files_sha256':{p.name:h(p) for p in output.iterdir() if p.is_file()},
        'request_binding':'冻结原件／问题／配置／协议；真实向量结果决定检索窗口。每次HTTP请求体在发送前预扣并保存哈希／字节；不改写生产输入。'}
    save(output/'plan.json',plan)
    return {'planned_http_upper':plan['planned_http_upper'],'max_model_http':80,'stage_id':plan['stage_id'],'steps':steps,'provider_http':0,'plan_sha256':hashed(plan)}

def verify_frozen(folder):
    folder=Path(folder).resolve();plan=read(folder/'plan.json')
    if source_identity()!=plan['application_source_identity'] or code_fingerprint()!=plan['code_fingerprint']:raise ValueError('n03_application_changed')
    if plan.get('provider_mode','real')!='real' and LEDGER.resolve()==(FORMAL/'data/rag-live-v8-20261019/request-ledger.db').resolve():
        raise ValueError('n03_mock_cannot_use_unique_live_ledger')
    if plan.get('annotation_state')!='sealed' or read(folder/'labels.json').get('sealed') is not True:raise ValueError('n03_annotation_seal_required')
    for name,digest in plan['input_files_sha256'].items():
        if h(folder/name)!=digest:raise ValueError('n03_frozen_input_changed_'+name)
    for name,digest in plan['runner_sources'].items():
        if h(ROOT/name)!=digest:raise ValueError('n03_runner_changed_'+name)
    settings=Settings.from_env(FORMAL/'.env')
    if any(getattr(settings,k)!=v for k,v in plan['settings'].items()):raise ValueError('n03_settings_changed')
    return plan,settings

class EmbeddingSpy:
    def __init__(self,runtime):self.runtime=runtime;self.provider=APIEmbedding(runtime.settings);self.settings=runtime.settings;self.calls=[];self.cursor=0
    @property
    def signature(self):return self.provider.signature
    @property
    def last_call_metadata(self):return self.provider.last_call_metadata
    def embed(self,texts):
        r=self.runtime
        if r.operation=='index':
            expected=r.plan['index_texts'][r.phase][self.cursor:self.cursor+len(texts)]
            if texts!=expected:raise ValueError('n03_index_input_changed')
            self.cursor+=len(texts);name='N03/'+r.phase+'/index';role='index'
        else:
            if texts!=[getattr(r,'expected_query',r.case['question'])]:
                error=ValueError('n03_query_input_changed');error.not_sent=True;raise error
            name='N03/'+r.phase+'/'+r.case['id']+'/query_embedding';role='query'
        self.calls.append(texts)
        with purpose(name,role):return self.provider.embed(texts)

class ModelSpy:
    def __init__(self,runtime):self.runtime=runtime;self.provider=DeepSeekModel(runtime.settings);self.calls=[];self.outputs=[]
    @property
    def last_call_metadata(self):return self.provider.last_call_metadata
    def invoke(self,system,user,stream=None):
        r=self.runtime;value=json.loads(user)
        if value.get('question')!=r.case['question']:raise ValueError('n03_writer_question_changed')
        kind='writer' if stream else 'composition_planner';name='N03/'+r.phase+'/'+r.case['id']+'/'+kind
        if not r.plan['http_slots'].get(name):raise ValueError('n03_unallocated_model_operation')
        self.calls.append({'system':system,'user':user,'purpose':name})
        with purpose(name):raw=self.provider.generate_stream(system,user,*stream) if stream else self.provider.generate(system,user)
        self.outputs.append(raw);return raw
    def generate(self,system,user):return self.invoke(system,user)
    def generate_stream(self,system,user,on_text,check_active):return self.invoke(system,user,(on_text,check_active))

class LiveRuntime(OfflineRuntime):
    def __init__(self,folder,plan,settings,phase,case=None,operation=None):
        super().__init__(Path(folder)/'runtime');self.plan=plan;self.settings=replace(settings,data_dir=self.directory,team_mode=True,session_cookie_secure=False)
        self.phase,self.case,self.operation=phase,case,operation;self.group='setup'
    def __enter__(self):
        from app.repository import Repository
        from fastapi.testclient import TestClient
        self.repository=Repository(self.directory/'docqa.db');self.repository.settings=self.settings;self.repository.initialize()
        self.embedding=EmbeddingSpy(self);self.model=ModelSpy(self)
        os.environ['DOCQA_DATA_DIR']=str(self.directory)
        self.stack.enter_context(patch.object(Settings,'from_env',return_value=self.settings))
        module=importlib.import_module('app.main')
        self.stack.enter_context(patch.object(module,'APIEmbedding',lambda _:self.embedding));self.stack.enter_context(patch.object(module,'DeepSeekModel',lambda _:self.model))
        app=module.create_app(self.settings)
        self.clients={name:self.stack.enter_context(TestClient(app)) for name in ['n03-admin','n03-customer']}
        from scripts.evaluate_markdown_lifecycle import PASSWORD
        for name in self.clients:
            response=self.http(name,'POST','/api/auth/login',{'username':name,'password':PASSWORD});assert response.status_code==200
            self.clients[name].headers['X-CSRF-Token']=response.json()['csrf_token']
        return self
    def http(self,*args,**kwargs):
        result=super().http(*args,**kwargs);self.entries[-1]['entry_group']=self.group;return result

def isolated_case_failure(row):
    task=row.get('task',{})
    return task.get('status')=='succeeded' or task.get('error_code','').startswith('output_')

def require_previous(folder,plan,step):
    for previous in plan['steps'][:plan['steps'].index(step)]:
        row=read(folder/'steps'/previous/'result.json')
        if not row['engineering_passed']:
            if plan.get('independent_case_failures') and previous.startswith('ask-') and isolated_case_failure(row):continue
            raise ValueError('n03_previous_step_failed')
        if previous.startswith('ask-'):
            review=read(folder/'steps'/previous/'semantic-review.json')
            if (review.get('passed') is not True and not plan.get('independent_case_failures')) or review.get('result_sha256')!=h(folder/'steps'/previous/'result.json'):
                raise ValueError('n03_prior_semantic_review_required')

def ask(r,base,document,step):
    from app.knowledge_worker import KnowledgeWorker
    from app.answer_stream_store import AnswerStreamStore
    r.group='case'
    key=r.plan['stage_id']+'/'+step
    response=r.http('n03-customer','POST','/api/chat/conversations',{'base_id':base,'request_key':key+'-conv','document_ids':[document]});assert response.status_code==201,response.text
    conv=response.json()['id'];body={'question':r.case['question'],'request_key':key+'-ask'}
    response=r.http('n03-customer','POST','/api/chat/conversations/'+conv+'/turns',body);assert response.status_code==202,response.text
    task=response.json()['task_id'];worker=KnowledgeWorker(r.settings,r.repository,LocalRuntime(),embedding=r.embedding,model=r.model)
    payload=json.loads(worker.tasks.row(task)['input_json']);r.expected_query=r.plan['query_texts'][r.case['id']]
    assert payload['retrieval_question']==r.expected_query
    worker.run_once(task)
    row=worker.tasks.row(task);detail=r.http('n03-customer','GET','/api/kb-tasks/'+task).json()
    result={'task':dict(row),'detail':detail,'model_inputs':r.model.calls,'model_outputs':r.model.outputs,'embedding_inputs':r.embedding.calls,'semantic_review_required':True}
    if row['status']!='succeeded':return {**result,'engineering_passed':False}
    snapshot=json.loads(row['input_json'])['documents'][0]['snapshot'];active=r.repository.get(document)
    assert snapshot['parse_version_id']==active.active_parse_version_id and snapshot['index_id']==active.active_index_id
    assert all(c['document_id']==document and c['parse_version_id']==active.active_parse_version_id for c in detail['result'].get('citations',[]))
    if detail['result']['status']!='answered':return {**result,'engineering_passed':False,'snapshot':snapshot,'expected_status_matched':False}
    assert detail['result']['citations']
    events=AnswerStreamStore(r.repository).read('knowledge',task);assert any(e['kind']=='block' for e in events)
    counts=len(r.embedding.calls),len(r.model.calls);r.group='verification'
    again=r.http('n03-customer','POST','/api/chat/conversations/'+conv+'/turns',body);assert again.json()['task_id']==task
    assert r.http('n03-customer','GET','/api/chat/conversations/'+conv).status_code==200
    assert r.http('n03-customer','GET','/api/kb-tasks/'+task+'/export').status_code==200
    assert counts==(len(r.embedding.calls),len(r.model.calls))
    return {**result,'engineering_passed':True,'snapshot':snapshot,'stream_blocks':sum(e['kind']=='block' for e in events),'stream_events_sha256':hashed(events),'duplicate_refresh_export_provider_delta':0}

def execute(folder,step):
    folder=Path(folder).resolve();plan,settings=verify_frozen(folder);assert step in plan['steps'];require_previous(folder,plan,step)
    out=folder/'steps'/step;out.mkdir(parents=True,exist_ok=False);stack=ExitStack();budget=None;result=None;r=None
    phase='new' if '-new' in step or step=='verify' else 'old';case=next((c for c in read(folder/'labels.json')['cases'] if step.endswith(c['id'])),None)
    try:
        budget=ApprovedCalls(LEDGER,plan,read(folder/'authorization.json'),out/'http')
        if step==plan.get('activate_on_step','index-old'):budget.activate()
        import app.deepseek as dm,app.embedding as em
        original=dm.OpenAI
        def sdk(**kwargs):
            kwargs['http_client']=httpx.Client(transport=MeteredTransport(budget,httpx.HTTPTransport(retries=0)),timeout=kwargs['timeout'],follow_redirects=False)
            return original(**kwargs)
        stack.enter_context(patch.object(dm,'OpenAI',sdk));stack.enter_context(patch.object(em,'OpenAI',sdk))
        previous=read(folder/'old-report.json');base,document=previous['base_id'],previous['document_id']
        r=LiveRuntime(folder,plan,settings,phase,case,'index' if step.startswith('index-') else 'query')
        with r:
            initial=retention(r,previous['parse_version_id'],previous['index_id'],[a['task_id'] for a in previous['answers']])
            assert initial==previous['retention']
            if step.startswith('index-'):
                index=r.index(document,'n03-real-'+step)
                with r.repository.connect() as db:revision=db.execute('SELECT revision FROM knowledge_bases WHERE id=?',(base,)).fetchone()[0]
                response=r.http('n03-admin','POST','/api/knowledge-bases/'+base+'/publish',{'expected_revision':revision});assert response.status_code==200,response.text
                active=r.repository.get(document);assert len(r.embedding.calls)==plan['http_slots']['N03/'+phase+'/index']
                result={'engineering_passed':True,'index_id':index,'parse_version_id':active.active_parse_version_id,
                    'provider_signature':r.embedding.signature,'index_texts_sha256':hashed(plan['index_texts'][phase]),
                    'real_index_retention':retention(r,active.active_parse_version_id,index,[a['task_id'] for a in previous['answers']])}
            elif step=='parse-new':
                version=r.parse(document,'n03-real-new-parse',True);assert version!=previous['parse_version_id']
                assert r.repository.get_parse_version(version).parser_version=='local-markdown-4'
                assert [c.text for c in r.repository.chunks(document,version)]==[c['text'] for c in read(plan['new_reference_report'])['chunks']]
                result={'engineering_passed':True,'parse_version_id':version,'provider_http_delta':0}
            elif step.startswith('ask-'):result=ask(r,base,document,step)
            else:
                old_index=read(folder/'steps/index-old/result.json')
                assert retention(r,previous['parse_version_id'],old_index['index_id'],[a['task_id'] for a in previous['answers']])==old_index['real_index_retention']
                for old_step in [s for s in plan['steps'] if s.startswith('ask-old-')]:
                    old=read(folder/'steps'/old_step/'result.json');current=r.http('n03-customer','GET','/api/kb-tasks/'+old['task']['id'])
                    assert current.status_code==200 and current.json()['result']==old['detail']['result']
                    assert r.http('n03-customer','GET','/api/documents/'+document+'/chunks?version_id='+previous['parse_version_id']).status_code==200
                    with r.repository.connect() as db:assert dict(db.execute('SELECT * FROM kb_tasks WHERE id=?',(old['task']['id'],)).fetchone())==old['task']
                assert r.http('n03-customer','GET','/api/documents/'+document+'/original').content==(folder/'source.md').read_bytes()
                for p in (Path(plan['old_baseline'])/'parse-results'/previous['parse_version_id']).rglob('*'):
                    if p.is_file():assert h(p)==h(folder/'runtime'/p.relative_to(Path(plan['old_baseline'])))
                assert not Path(previous['sentinel']).exists()
                result={'engineering_passed':True,'old_real_answers_and_sources_preserved':True,'old_offline_rows_preserved':True,'original_unchanged':True,'code_not_executed':True,'provider_http_delta':0}
            assert retention(r,previous['parse_version_id'],previous['index_id'],[a['task_id'] for a in previous['answers']])==initial
            assert source_identity()==plan['application_source_identity']
        result.update(step=step,entry_http_calls=len(r.entries),entries=r.entries,source_identity=source_identity(),plan_sha256=hashed(plan),model_http=budget.report(),real_provider=plan.get('provider_mode','real')=='real')
        save(out/'result.json',result)
        if not result['engineering_passed'] and not (plan.get('independent_case_failures') and step.startswith('ask-') and
                isolated_case_failure(result) and not budget.stopped):raise ValueError('n03_task_failed_'+step)
        if step=='verify':
            failed=any(not read(folder/'steps'/s/'result.json')['engineering_passed'] or not read(folder/'steps'/s/'semantic-review.json').get('passed',False)
                for s in plan['steps'] if s.startswith('ask-'))
            budget.stop('complete_with_failed_cases' if failed else 'complete')
        print(json.dumps({k:result[k] for k in ['step','engineering_passed','entry_http_calls'] }|{'model_http_used':budget.report()['used'],'semantic_review_required':result.get('semantic_review_required',False)},ensure_ascii=False))
        return result
    except BaseException:
        if budget:budget.stop('stopped_unexpected_result')
        import traceback
        (out/'first-failure.txt').write_text(traceback.format_exc(),'utf-8')
        if result is None:save(out/'result.json',{'step':step,'engineering_passed':False,'model_http':budget.report() if budget else {'used':0},'entries':r.entries if r else []})
        raise
    finally:stack.close();r.stack.close() if r else None

if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    f=sub.add_parser('freeze');f.add_argument('--review',required=True);f.add_argument('--seal',required=True);f.add_argument('--output',required=True)
    r=sub.add_parser('execute');r.add_argument('--frozen',required=True);r.add_argument('--step',required=True)
    a=p.parse_args()
    if a.command=='freeze':print(json.dumps(freeze(a.review,a.seal,a.output),ensure_ascii=False))
    else:execute(a.frozen,a.step)
