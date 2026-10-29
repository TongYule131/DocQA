"""已封存整问标注的离线生产编排评估；不访问供应商或正式数据。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import re
import socket
import sys
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
VERSION = 'intent-responsibility-execution-v1'
SCENARIOS = {'normal', 'tool_complete', 'tool_missing', 'foreign_temporary', 'revoke_before_worker',
    'waiting_upload', 'tool_credentials_missing', 'tool_continue', 'tool_change_known_parameter',
    'cancelled_confirmation', 'foreign_filter', 'configuration_mismatch', 'tool_http_failure',
    'tool_uncertain', 'tool_disabled_confirmation', 'foreign_confirmation', 'expired_temporary'}


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def new_output(path):
    path = Path(path).resolve()
    if not path.is_relative_to((ROOT/'data').resolve()) or path == (ROOT/'data').resolve():
        raise ValueError('output_must_be_new_child_of_project_data')
    path.mkdir(parents=True, exist_ok=False)
    return path


def validate_inputs(annotations, inputs):
    cases = annotations['cases']
    if annotations.get('sealed') is not True or annotations.get('review_status') != 'accepted_by_user':
        raise ValueError('human_annotation_seal_required')
    ids = [c['id'] for c in cases]
    if len(ids) != 36 or len(set(ids)) != 36 or set(ids) != set(inputs['cases']):
        raise ValueError('case_identity_mismatch')
    if any(type(c.get('classification_score_eligible')) is not bool for c in cases):
        raise ValueError('explicit_classification_boolean_required')
    if set(inputs['corpora']) != {'development', 'calibration', 'holdout'}:
        raise ValueError('split_inputs_missing')
    seen = set()
    for split, docs in inputs['corpora'].items():
        for doc in docs:
            raw = '\n'.join(doc['paragraphs'])
            if not raw or len(raw) > 10000 or digest(raw) in seen:
                raise ValueError('invalid_or_cross_split_duplicate_source')
            seen.add(digest(raw))
    for case in cases:
        row = inputs['cases'][case['id']]
        if row['scenario'] not in SCENARIOS or set(row.get('settings', {})) - {'rag_min_score'}:
            raise ValueError('unsupported_fixture_scenario_or_configuration')
        if case['split'] not in inputs['corpora']:
            raise ValueError('case_split_missing')


def freeze(annotations_path, seal_path, inputs_path, output):
    annotations, seal, inputs = read(annotations_path), read(seal_path), read(inputs_path)
    if seal.get('status') != 'sealed' or sha256(Path(annotations_path).read_bytes()).hexdigest() != seal['candidate_sha256']:
        raise ValueError('annotation_manifest_mismatch')
    authorization=Path(seal_path).parent/seal['user_authorization_file']
    if sha256(authorization.read_bytes()).hexdigest()!=seal['user_authorization_sha256']:
        raise ValueError('annotation_authorization_changed')
    validate_inputs(annotations, inputs)
    out = new_output(output)
    from app.runtime import source_identity
    bundle = {'version': VERSION, 'annotations': annotations, 'inputs': inputs,
        'annotation_sha256': seal['candidate_sha256'], 'annotation_seal_sha256': sha256(Path(seal_path).read_bytes()).hexdigest(),
        'inputs_sha256': sha256(Path(inputs_path).read_bytes()).hexdigest(),
        'runner_sha256': sha256(Path(__file__).read_bytes()).hexdigest(), 'application_source_identity': source_identity(),
        'provider_http': 0, 'real_semantic_or_language_certification': False}
    bundle['input_hash'] = digest({k:v for k,v in bundle.items() if k != 'application_source_identity'})
    # 原文字节单独落盘并绑定，避免把场景说明当成已冻结业务原件。
    sources = out/'source-documents'; sources.mkdir()
    bundle['source_files'] = {}
    for split, docs in inputs['corpora'].items():
        for doc in docs:
            name = split+'-'+doc['key']+'.txt'
            content = '\n'.join(doc['paragraphs']).encode('utf-8')
            (sources/name).write_bytes(content)
            bundle['source_files'][name] = sha256(content).hexdigest()
    (out/'capture-runner.py').write_bytes(Path(__file__).read_bytes())
    (out/'input-fixtures.json').write_bytes(Path(inputs_path).read_bytes())
    write(out/'bundle.json', bundle)
    return {'input_hash':bundle['input_hash'], 'case_count':36, 'provider_http':0, 'output':str(out)}


class OfflineEmbedding:
    signature = 'intent-responsibility-synthetic-2d-v1'
    def __init__(self, settings, vector=None):
        self.settings, self.calls, self.query_vector = settings, [], vector
    def embed(self, texts):
        self.calls.append(list(texts))
        return [list(self.query_vector or [1., 0.]) for _ in texts]


class FrozenModel:
    """只绑定冻结引述的实际选项ID，不读取预期动作、状态或调用上界。"""
    def __init__(self, fixture):
        self.fixture, self.calls, self.outputs = fixture, [], []
    @staticmethod
    def select_units(catalog, texts):
        selected=[]
        for text in texts:
            exact=next((u for u in catalog if text in u['text']),None)
            pieces=[exact] if exact else [u for u in catalog if u['text'] and u['text'] in text]
            if not exact and ''.join(u['text'] for u in pieces)!=text:
                raise ValueError('frozen_quote_not_fully_visible_to_stub')
            for unit in pieces:
                key={'ref':unit['ref'],'unit':unit['unit']}
                if key not in selected:selected.append(key)
        return selected
    def generate(self, system, user):
        self.calls.append({'system':system, 'user':user})
        value = json.loads(user)
        if 'schema' in value:
            tool = self.fixture['tool']
            result = tool.get('continuation_arguments', tool['initial_arguments']) if value.get('known_arguments') else tool['initial_arguments']
        elif 'evidence_units' in value:
            selected = self.select_units([{'ref':u['ref'],'unit':u['unit'],
                'text':''.join(p['text'] for p in u['passages'])} for u in value['evidence_units']], self.fixture['response']['quote_texts'])
            missing = self.fixture['response']['missing_part_ids']
            parts = [p['part_id'] for p in value['question_parts'] if p['part_id'] not in missing]
            role = value['organization_brief']['first_role']
            groups={}
            for unit in selected:groups.setdefault(unit['ref'],[]).append(unit)
            result = {'blocks':[{'role':role, 'focus':'回答所问条件与关系', 'part_ids':parts,
                'groups':[{'label':'原文依据'+str(i), 'units':units} for i,units in groups.items()]}],
                'missing_part_ids':missing}
        else:
            response = self.fixture['response']; selected = []
            def unit_text(reference, parts):
                texts=[]
                for part in parts:
                    ref,span,first,last=part if len(part)==4 else (reference['ref'],*part)
                    source=next(r for r in value['references'] if r['ref']==ref)
                    window=next(w for w in source['content'] if w['span']==span)
                    texts.extend(window['segments'][str(i)] for i in range(first,last+1))
                return ''.join(texts)
            selected=self.select_units([{'ref':r['ref'],'unit':int(key),'text':unit_text(r,parts)}
                for r in value['references'] for key,parts in r.get('quote_units',{}).items()],response['quote_texts'])
            parts = value['question_parts']; missing = response['missing_part_ids']
            ids = [p['part_id'] for p in parts if p['part_id'] not in missing]
            gaps = ['本次资料缺少该子问的原文依据。' for p in parts if p['part_id'] in missing]
            role = value.get('composition_plan', {}).get('blocks', [{'role':'evidence'}])[0]['role']
            result = {'status':'partial' if missing else 'answered', 'conclusion':[{
                'text':response['text'], 'part_ids':ids, 'claim_type':response['claim_type'],
                'context':[], 'evidence':selected}], 'explanation':[], 'clarification_questions':[],
                'missing_points':gaps, 'answer_parts':[{'part_id':p['part_id'],
                    'state':'missing_evidence' if p['part_id'] in missing else 'answered',
                    'missing_detail':'本次资料缺少该子问的原文依据。' if p['part_id'] in missing else ''} for p in parts],
                'presentation':{'blocks':[{'role':role, 'kind':'paragraph', 'fact_indices':[0]}]}}
        raw = json.dumps(result, ensure_ascii=False)
        self.outputs.append(raw)
        return raw
    def generate_stream(self, system, user, on_text, check_active):
        raw = self.generate(system, user)
        for start in range(0, len(raw), 80):
            check_active(); on_text(raw[start:start+80])
        return raw


class LocalRuntime:
    def check(self):return {}


@contextmanager
def no_external_network():
    original = socket.socket.connect
    def connect(sock, address):
        caller = sys._getframe(1)
        if caller.f_globals.get('__name__') == 'socket' and caller.f_code.co_name == 'socketpair':
            return original(sock, address)
        raise AssertionError('external_network_forbidden')
    with patch.object(socket.socket, 'connect', connect), patch.object(socket, 'create_connection', side_effect=AssertionError('external_network_forbidden')):
        yield


class CaseRuntime:
    def __init__(self, case, fixture, corpus, settings, directory):
        self.case, self.fixture, self.corpus, self.settings, self.directory = case, fixture, corpus, settings, directory
        self.entries, self.business_calls, self.tasks_run = [], [], []
        self.phase = 'setup'; self.stack = ExitStack()
        self.embedding, self.model = OfflineEmbedding(settings), FrozenModel(fixture)
    def network_guard(self):return no_external_network()
    def __enter__(self):
        from app.config import Settings
        from app.repository import Repository, now_iso
        from app.product_access import ProductAccess
        from app.vector_index import DocumentIndex
        from app.schemas import Document, Chunk, SourceLocation
        from app.knowledge_worker import KnowledgeWorker
        from fastapi.testclient import TestClient
        import httpx
        self.stack.enter_context(self.network_guard())
        self.repository = Repository(self.directory/'docqa.db'); self.repository.settings=self.settings; self.repository.initialize()
        index = DocumentIndex(self.repository, self.embedding)
        access = ProductAccess(self.repository, self.settings, index)
        self.users = {}
        for name, role in [('admin','admin'),('alice','customer'),('bob','customer')]:
            self.users[name] = access.create_user(name, name, 'Synthetic-only-password-2026', role)
        self.doc_ids = []
        for doc in self.corpus:
            identifier = self.case['split']+'-'+doc['key']; self.doc_ids.append(identifier)
            raw = '\n'.join(doc['paragraphs']).encode('utf-8')
            upload = self.directory/'uploads'; upload.mkdir(exist_ok=True)
            (upload/(identifier+'.txt')).write_bytes(raw)
            self.repository.create(Document(id=identifier, filename=doc['filename'], size=len(raw), created_at=now_iso(), status='uploaded', format='txt'))
            self.repository.finish_parse(identifier, len(doc['paragraphs']), [Chunk(id=identifier+'-'+str(i), document_id=identifier,
                parse_version_id=identifier+'-v1', text=text, page=None, order_index=i-1, chunk_type='text',
                sources=[SourceLocation(format='txt', line_start=i, line_end=i)]) for i,text in enumerate(doc['paragraphs'], 1)])
            index.build(identifier)
        self.stack.enter_context(patch.object(Settings, 'from_env', return_value=self.settings))
        main = importlib.import_module('app.main')
        self.stack.enter_context(patch.object(main, 'APIEmbedding', lambda _:self.embedding))
        self.stack.enter_context(patch.object(main, 'DeepSeekModel', lambda _:self.model))
        self.app = main.create_app(self.settings)
        self.clients = {name:self.stack.enter_context(TestClient(self.app)) for name in self.users}
        for name in self.users:
            response = self.http('POST','/api/auth/login', {'username':name,'password':'Synthetic-only-password-2026'}, name)
            self.clients[name].headers['X-CSRF-Token'] = response.json()['csrf_token']
        self.base = self.http('POST','/api/knowledge-bases', {'name':self.case['split']+'合成资料'}, 'admin').json()
        for identifier in self.doc_ids:
            self.http('POST',f'/api/knowledge-bases/{self.base["id"]}/documents', {'document_id':identifier}, 'admin')
        for name in ('alice','bob'):
            self.http('PUT',f'/api/knowledge-bases/{self.base["id"]}/members/{self.users[name]["id"]}', {'enabled':True}, 'admin')
        published = self.http('POST',f'/api/knowledge-bases/{self.base["id"]}/publish', {'expected_revision':0}, 'admin')
        if published.status_code != 200:raise ValueError('fixture_publish_failed')
        self.worker = KnowledgeWorker(self.settings, self.repository, LocalRuntime(), embedding=self.embedding, model=self.model)
        original_client = httpx.Client
        import app.knowledge_tools as tool_module
        # 只模拟业务工具；保留httpx.Client类型，供真实供应商SDK校验客户端。
        self.stack.enter_context(patch.object(tool_module,'httpx',SimpleNamespace(
            Client=lambda **kw:original_client(transport=httpx.MockTransport(self.tool_http),**kw),
            Timeout=httpx.Timeout,TimeoutException=httpx.TimeoutException)))
        if self.fixture.get('tool'):self.prepare_tool()
        self.embedding.query_vector = self.fixture.get('query_vector')
        return self
    def __exit__(self, *args):return self.stack.__exit__(*args)
    def http(self, method, path, body=None, actor='alice', **kwargs):
        response = self.clients[actor].request(method, path, **({'json':body} if body is not None else {}), **kwargs)
        # 登录响应不保留Cookie／CSRF；其余全部为合成业务输入和响应。
        self.entries.append({'phase':self.phase, 'method':method, 'path':path, 'actor':actor,
            'http_status':response.status_code, 'request':None if path=='/api/auth/login' else body,
            'response':None if path=='/api/auth/login' else response.json(), 'response_sha256':sha256(response.content).hexdigest()})
        return response
    def counts(self):
        with self.repository.connect() as db:
            tasks=db.execute('SELECT COUNT(*) FROM kb_tasks').fetchone()[0]
            calls=db.execute('SELECT COUNT(*) FROM kb_task_calls').fetchone()[0]
        return {'embedding':len(self.embedding.calls), 'generation':len(self.model.calls),
                'tool_http':len(self.business_calls), 'tasks':tasks, 'ledger_rows':calls,
                'entry_http_calls':len(self.entries)}
    def conversation(self, actor='alice', base=None, documents=None):
        selected = self.fixture['selected_documents']
        ids = self.doc_ids if selected=='*' else [self.case['split']+'-'+selected]
        response=self.http('POST','/api/chat/conversations', {'base_id':base or self.base['id'],
            'request_key':'conv-'+str(len(self.entries)), 'document_ids':ids if documents is None else documents}, actor)
        if response.status_code != 201:raise ValueError('fixture_conversation_failed')
        return response.json()['id']
    def prepare_tool(self):
        from app.knowledge_tools import ToolRegistry
        from app.intent_management import IntentManagement
        tool=self.fixture['tool']; auth='DOCQA_MCP_OFFLINE_ABSENT' if self.fixture['scenario']=='tool_credentials_missing' else None
        if auth:self.stack.enter_context(patch.dict(os.environ, {auth:''}))
        self.tool_id=self.case['split']+'-readonly-tool'
        ToolRegistry(self.repository).save({'id':self.tool_id,'name':'隔离只读查询','endpoint':'http://127.0.0.1:9999/mcp',
            'tool_name':'read_only','auth_env':auth,'input_schema':{'type':'object','properties':tool['properties'],'required':tool['required']},
            'protocol_version':'2025-11-25','read_only':True,'enabled':True})
        IntentManagement(self.repository).save({'code':'isolated-query','name':'隔离只读查询','kind':'MCP',
            'base_id':self.base['id'],'tool_id':self.tool_id,'examples':[tool['command']],'enabled':True})
    def tool_http(self, request):
        import httpx
        value=json.loads(request.content); self.business_calls.append(value)
        scenario=self.fixture['scenario']
        if scenario=='tool_http_failure':return httpx.Response(502)
        if scenario=='tool_uncertain':raise httpx.ReadTimeout('isolated_timeout', request=request)
        if value['method']=='initialize':result={'protocolVersion':'2025-11-25'}
        elif value['method']=='notifications/initialized':return httpx.Response(202)
        else:result={'content':[{'type':'text','text':'隔离合成工具结果'}]}
        return httpx.Response(200, json={'jsonrpc':'2.0','id':value['id'],'result':result})
    def submit(self, conv, question, key='ask', actor='alice'):
        return self.http('POST',f'/api/chat/conversations/{conv}/turns', {'question':question,'request_key':key}, actor)
    def run_worker(self, task):
        self.worker.run_once(task); self.tasks_run.append(task)
        return self.worker.tasks.row(task)
    def resolve(self, conv, clarification, values, key='confirm', actor='alice'):
        return self.http('POST',f'/api/chat/conversations/{conv}/clarifications/{clarification}/resolve', {'names':values,'request_key':key}, actor)
    def preserved_assets(self, old_task_id):
        """仅隔离库：绑定旧任务／结果、原件字节和解析／索引实际行。"""
        def safe(value):return value.hex() if isinstance(value,bytes) else value
        with self.repository.connect() as db:
            values={name:digest([[safe(v) for v in row] for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')])
                for name in ('documents','parse_versions','chunks','embedding_vectors_v2','embedding_indexes')}
            values['old_task']=digest(dict(db.execute('SELECT * FROM kb_tasks WHERE id=?',(old_task_id,)).fetchone()))
        values['originals']={p.name:sha256(p.read_bytes()).hexdigest() for p in sorted((self.directory/'uploads').iterdir())}
        return values


def observe(runtime):
    """场景只准备生产入口的前置；最终动作／费用从HTTP、任务和实际结果读取。"""
    r=runtime; case=r.case; f=r.fixture; scenario=f['scenario']; question=case['question']
    from app.intent_management import IntentManagement
    from app.repository import now_iso
    from app.schemas import Document
    conv=None; task_id=None; response=None; row=None; prior_task=None; phase_counts={}; bench=None
    preservation=None; waiting_replay=None
    if scenario in ('foreign_temporary','expired_temporary'):
        owner='bob' if scenario=='foreign_temporary' else 'alice'
        private=r.http('POST','/api/chat/temporary',{'request_key':'temporary'},owner).json()
        if scenario=='expired_temporary':
            with r.repository.connect() as db:db.execute('UPDATE knowledge_bases SET expires_at=? WHERE id=?',('2000-01-01T00:00:00+00:00',private['id']))
        request={'base_id':private['id'],'request_key':'open','document_ids':[]}
    elif scenario=='foreign_filter':
        foreign=r.http('POST','/api/knowledge-bases',{'name':'独立外库'},'admin').json()
        r.repository.create(Document(id='foreign-document',filename='外库资料.txt',size=1,created_at=now_iso(),status='uploaded',format='txt'))
        r.http('POST',f'/api/knowledge-bases/{foreign["id"]}/documents',{'document_id':'foreign-document'},'admin')
        request={'base_id':r.base['id'],'request_key':'foreign','document_ids':['foreign-document']}
    elif scenario=='waiting_upload':
        import io
        private=r.http('POST','/api/chat/temporary',{'request_key':'waiting-temp'}).json()
        raw='\n'.join(r.corpus[0]['paragraphs']).encode('utf-8')
        response=r.http('POST','/api/ingestions',actor='alice', data={'base_id':private['id']},
            files={'file':('排班说明.txt',io.BytesIO(raw),'text/plain')},headers={'Idempotency-Key':'waiting-upload'})
        if response.status_code!=202:raise ValueError('fixture_upload_failed')
        document=response.json()['document_id']; conv=r.conversation(base=private['id'],documents=[document])
    else:
        actor='bob' if scenario=='foreign_confirmation' else 'alice'
        conv=r.conversation(actor=actor)
        if f.get('entity_names') or f.get('clear_entity_names'):
            names=f.get('entity_names') or f['clear_entity_names']
            r.http('PATCH',f'/api/chat/conversations/{conv}/entities',{'expected_revision':0,'names':names,'mode':'replace'})
            if f.get('clear_entity_names'):
                detail=r.http('GET',f'/api/chat/conversations/{conv}').json()
                r.http('PATCH',f'/api/chat/conversations/{conv}',{'expected_revision':detail['revision'],'clear_memory':True})
        if f.get('top_k'):
            # 该节点只收紧资源预算；不作为本题的分类标签或答案知识点。
            IntentManagement(r.repository).save({'code':'restricted-context','name':'证据预算限制','kind':'KB',
                'base_id':r.base['id'],'examples':[question],'enabled':True,'top_k':f['top_k']})
        if scenario in ('tool_continue','tool_change_known_parameter','cancelled_confirmation','tool_disabled_confirmation','foreign_confirmation'):
            original=r.submit(conv,f['tool']['command'],'initial',actor).json(); prior_task=r.run_worker(original['task_id'])
            prior_result=json.loads(prior_task['result_json']); clar=prior_result['clarification']['id']
            if scenario=='cancelled_confirmation':r.http('POST',f'/api/chat/conversations/{conv}/clarifications/{clar}/cancel')
            if scenario=='tool_disabled_confirmation':
                with r.repository.connect() as db:db.execute('UPDATE mcp_tools SET enabled=0,revision=revision+1 WHERE id=?',(r.tool_id,))
            if scenario=='foreign_confirmation':conv=r.conversation()
        if scenario=='configuration_mismatch':
            old=r.submit(conv,question,'older-success').json(); old_task=r.run_worker(old['task_id'])
            if old_task['status']!='succeeded':raise ValueError('preservation_requires_existing_success')
            preservation={'old_task_id':old_task['id'],'before':r.preserved_assets(old_task['id'])}
    before=r.counts(); r.phase='case'
    if scenario in ('foreign_temporary','expired_temporary','foreign_filter'):
        response=r.http('POST','/api/chat/conversations',request)
    elif scenario in ('tool_continue','tool_change_known_parameter','cancelled_confirmation','tool_disabled_confirmation','foreign_confirmation'):
        response=r.resolve(conv,clar,f['tool'].get('confirmed_values',['商品甲']))
        if response.status_code==200 and response.json().get('task_id'):task_id=response.json()['task_id']
    else:
        response=r.submit(conv,question)
        if response.status_code==202:task_id=response.json().get('task_id')
    if scenario=='waiting_upload':
        initial=r.counts(); replay=r.submit(conv,question).json();r.http('GET',f'/api/chat/conversations/{conv}')
        current=r.counts()
        waiting_replay={'same_submission':replay['id']==response.json()['id'],
            'new_tasks':current['tasks']-initial['tasks'],
            'new_calls':sum(current[k]-initial[k] for k in ('embedding','generation','tool_http'))}
    if task_id:
        if scenario=='revoke_before_worker':
            with r.repository.connect() as db:db.execute('UPDATE knowledge_bases SET revoked_at=? WHERE id=?',(now_iso(),r.base['id']))
        if scenario=='configuration_mismatch':
            r.worker.settings=replace(r.settings,question_context_max_chars=r.settings.question_context_max_chars+1)
        row=r.run_worker(task_id)
        if case['id']=='IR-D12':
            initial=r.counts(); phase_counts['initial_submission']={k:initial[k]-before[k] for k in ('embedding','generation','tool_http','entry_http_calls')}
            r.submit(conv,question); r.http('GET',f'/api/chat/conversations/{conv}')
            after=r.counts(); phase_counts['repeat_submission_and_refresh']={k:after[k]-initial[k] for k in phase_counts['initial_submission']}
        if scenario=='tool_uncertain':r.run_worker(task_id)  # 需要人工介入的任务不能自动重发。
    if preservation:
        preservation['after']=r.preserved_assets(preservation['old_task_id'])
        preservation['old_task_status']=r.worker.tasks.row(preservation['old_task_id'])['status']
    after=r.counts(); result=json.loads(row['result_json']) if row and row['result_json'] else None
    payload=json.loads(row['input_json']) if row else None
    # 带同一材料/实体上下文复核测试台；此处不执行检索或工具。
    if payload and scenario not in ('revoke_before_worker','configuration_mismatch','tool_continue','tool_change_known_parameter'):
        r.phase='verification'
        b=r.http('POST','/api/admin/intents/test',{'questions':[question],'scope':'knowledge','base_id':r.base['id'],
            'context':payload['conversation_context']['entity_context']},'admin')
        if b.status_code==200:bench=b.json()['results'][0]
    presented_result=result
    if row and scenario!='revoke_before_worker':
        r.phase='verification'
        detail=r.http('GET',f'/api/kb-tasks/{task_id}')
        if detail.status_code==200:presented_result=detail.json().get('result')
    if response.status_code>=400:
        action='stop' if scenario=='cancelled_confirmation' else 'deny'; outcome='denied'; kind='none'
    elif response.json().get('status')=='waiting':action,outcome,kind='wait','waiting','none'
    elif response.json().get('status')=='failed' and not task_id:action,outcome,kind='stop','submission_failed','none'
    elif row and row['status'] in ('cancelled','failed') and scenario in ('revoke_before_worker','configuration_mismatch'):
        action,outcome,kind='stop',row['status'],'none'
    else:
        route=(payload or {}).get('route',{}); kind=route.get('kind'); action=(result or {}).get('execution',{}).get('action') or (payload or {}).get('execution',{}).get('action')
        outcome=result['status'] if result else row['status'] if row else 'unobserved'
    with r.repository.connect() as db:
        calls=[dict(c) for c in db.execute('SELECT * FROM kb_task_calls WHERE task_id=? ORDER BY sequence_no',(task_id,))] if task_id else []
        original_confirmation=db.execute('SELECT state FROM conversation_clarifications WHERE task_id=?',(prior_task['id'],)).fetchone() if prior_task else None
        parameters=db.execute("SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name='tool_parameters'",(task_id,)).fetchone() if task_id else None
    return {'id':case['id'],'split':case['split'],'question':question,'actual':{'route_kind':kind,'action':action,'outcome':outcome},
        'main_calls':{k:after[k]-before[k] for k in ('embedding','generation','tool_http')},
        'entry_http_calls':{'setup':sum(e['phase']=='setup' for e in r.entries),'case':sum(e['phase']=='case' for e in r.entries),'verification':sum(e['phase']=='verification' for e in r.entries)},
        'new_tasks':after['tasks']-before['tasks'],'new_call_rows':after['ledger_rows']-before['ledger_rows'],
        'phase_calls':phase_counts,'entries':r.entries,'task':row,'payload':payload,'result':result,'presented_result':presented_result,'bench':bench,
        'preservation':preservation,'waiting_replay':waiting_replay,
        'calls':calls,'business_http':r.business_calls,'model_inputs':r.model.calls,'model_outputs':r.model.outputs,
        'tool_parameter_checkpoint':json.loads(parameters[0]) if parameters else None,
        'original_confirmation_state':original_confirmation[0] if original_confirmation else None,
        'expected_document_ids':[] if not payload or payload['route']['kind']!='KB' else r.doc_ids if f['selected_documents']=='*' else [case['split']+'-'+f['selected_documents']],
        'corpus_texts':{case['split']+'-'+d['key']:'\n'.join(d['paragraphs']) for d in r.corpus}}


def score(case, observation):
    expected=case['expected']; actual=observation['actual']; result=observation.get('result') or {}; payload=observation.get('payload') or {}
    route=payload.get('route',{}); citations=result.get('citations',[])
    resources=all(c['document_id'] in observation['expected_document_ids'] and bool(c.get('quote')) and
        c.get('quote','') in observation['corpus_texts'][c['document_id']] for c in citations)
    if route.get('kind')=='KB':resources &= [d['document_id'] for d in payload.get('documents',[])]==observation['expected_document_ids']
    if expected['action'] in ('deny','stop','wait'):resources &= observation['new_tasks']==0 or case['id'] in ('IR-D11','IR-C12')
    if expected['action']=='retrieve' and actual['outcome'] in ('answered','partial'):resources &= bool(citations)
    if route.get('kind')=='MCP':
        configured=case['split']+'-readonly-tool'
        resources &= route.get('tool_id')==configured
        if actual['outcome']=='answered':
            resources &= result.get('tool_source',{}).get('id')==configured and not citations
            requests=observation.get('business_http',[])
            resources &= [r.get('method') for r in requests]==['initialize','notifications/initialized','tools/call']
            resources &= bool(requests) and requests[-1].get('params',{}).get('arguments')==result.get('tool_arguments')
    parts=result.get('answer_parts',[])
    # 只给真正执行了字段级断言的项目记通过；文本语义不能由默认True冒充。
    objects=None
    if case['id']=='IR-D05':objects=any('分层检验法' in b['names'] for b in payload.get('understanding',{}).get('bindings',[]))
    if expected['action']=='clarify':objects=bool(result.get('clarification'))
    if case['id']=='IR-C06':objects=observation['original_confirmation_state']=='resolved' and 'clarification' not in result and result.get('tool_arguments')=={'point':'乙站','date':'本周一'}
    if case['id']=='IR-D09':objects=result.get('execution',{}).get('state')=='needs_parameters'
    if case['id']=='IR-D08':objects=result.get('tool_arguments')=={'warehouse':'甲库','product':'货甲'}
    if case['id']=='IR-H12':objects=any(p['part_id']=='p2' and p['state']=='missing_evidence' for p in parts)
    calls=observation['main_calls']; within=all(calls[k]<=expected['calls'][k] for k in calls)
    safety=resources and within
    if expected['action'] in ('reply','clarify','split_request','source_unavailable','deny','stop','wait'):
        safety &= not citations and all(v==0 for v in calls.values())
    entries=[e for e in observation['entries'] if e['phase']=='case']
    main=entries[0] if entries else {}
    body=main.get('response') or {}
    def field(path):
        if path=='new_business_task':return observation['new_tasks']>0
        if path=='entry.http_status':return main.get('http_status')
        if path=='entry.outcome':return 'denied' if main.get('http_status',0)>=400 else 'accepted'
        if path=='error_code':return (observation.get('task') or {}).get('error_code')
        if path=='result':return observation.get('result')
        prefix,*keys=path.split('.')
        value={'task':observation.get('task'),'result':observation.get('presented_result'),
               'chat_submission':body}.get(prefix)
        for key in keys:value=value.get(key) if isinstance(value,dict) else None
        return value
    state_contract=(all(field(k)==v for k,v in expected.get('status_contract',{}).items())
        and all(k not in result for k in expected.get('result_fields_absent',[]))) if (
        expected.get('status_contract') or expected.get('result_fields_absent')) else None
    http_contract=None
    if expected.get('entry_http'):
        e=expected['entry_http']
        pattern=re.sub(r'\\\{[^}]+\\\}',r'[^/]+',re.escape(e['path_template']))
        http_contract=(main.get('method')==e['method'] and bool(re.fullmatch(pattern,main.get('path','')))
            and main.get('http_status')==e['http_status'] and body.get('status')==e['response_status']
            and (body.get('error_code') or body.get('code'))==e['response_error_code']
            and body.get('detail')==e['response_detail'] and observation['new_tasks']==e['new_business_tasks'])
    phase_contract=(all(all(observation['phase_calls'].get(p,{}).get(k)==v for k,v in counts.items())
        for p,counts in expected['calls_by_phase'].items()) if expected.get('calls_by_phase') else None)
    bench=observation.get('bench'); parity=None if bench is None else bench['execution']==payload.get('execution')
    dimensions={'classification': None if not case['classification_score_eligible'] else
        (actual['route_kind'],actual['action'])==(expected['route_kind'],expected['action']),
        'action':actual['action']==expected['action'],'resources_and_sources':bool(resources),'objects_parameters_clarification':objects,
        'outcome':actual['outcome']==expected['outcome'],'calls':calls==expected['calls'],'call_upper_bound':within,
        'safety':bool(safety),'bench_submission_parity':parity,'status_namespaces':state_contract,
        'entry_http_contract':http_contract,'call_phase_contract':phase_contract}
    return {'id':case['id'],'split':case['split'],'classification_score_eligible':case['classification_score_eligible'],
        'expected_action':expected['action'],'actual':actual,'calls':calls,'expected_calls':expected['calls'],
        'entry_http_calls':observation['entry_http_calls'],'dimensions':dimensions,
        'engineering_passed':all(v is not False for v in dimensions.values()),
        'semantic_correctness':'not_evaluated_stubbed_provider','language_quality':'not_evaluated_stubbed_provider'}


def run(bundle_path, split, output, case_ids=None):
    bundle=read(bundle_path); validate_inputs(bundle['annotations'],bundle['inputs'])
    selected=select_cases(bundle['annotations']['cases'],split,case_ids)
    if bundle['runner_sha256']!=sha256(Path(__file__).read_bytes()).hexdigest():raise ValueError('runner_changed_refreeze_required')
    expected_hash=digest({k:v for k,v in bundle.items() if k not in ('application_source_identity','input_hash','source_files')})
    if bundle['input_hash']!=expected_hash:raise ValueError('bundle_identity_mismatch')
    for name, h in bundle['source_files'].items():
        if sha256((Path(bundle_path).parent/'source-documents'/name).read_bytes()).hexdigest()!=h:raise ValueError('frozen_source_changed')
    from app.runtime import source_identity
    if source_identity()!=bundle['application_source_identity']:raise ValueError('application_changed_refreeze_required')
    from app.config import Settings
    out=new_output(output); reports=[]
    for case in selected:
        directory=out/case['id']; directory.mkdir()
        fixture=bundle['inputs']['cases'][case['id']]
        settings=Settings(data_dir=directory,deepseek_api_key='offline-only',embedding_api_key='offline-only',
            **{**bundle['inputs']['settings'],**fixture.get('settings',{})})
        runtime=CaseRuntime(case,fixture,bundle['inputs']['corpora'][split],settings,directory)
        try:
            with runtime:
                observed=observe(runtime)
                write(directory/'observed.json',observed)
                report=score(case,observed)
        except Exception as exc:
            runtime.stack.close()
            import traceback
            (directory/'harness-error.txt').write_text(traceback.format_exc(),'utf-8')
            report={'id':case['id'],'split':split,'engineering_passed':False,'harness_error':type(exc).__name__,
                'dimensions':{},'semantic_correctness':'not_evaluated','language_quality':'not_evaluated'}
        reports.append(report); print(json.dumps({'id':case['id'],'passed':report['engineering_passed'],'harness_error':report.get('harness_error')},ensure_ascii=False),flush=True)
    result={'version':VERSION,'input_hash':bundle['input_hash'],'annotation_sha256':bundle['annotation_sha256'],
        'application_source_identity':source_identity(),'runner_sha256':bundle['runner_sha256'],'split':split,
        'cases':reports,'engineering_passed':sum(r['engineering_passed'] for r in reports),'total':len(reports),
        'explicit_case_ids':case_ids,'complete_split_executed':len(selected)==sum(c['split']==split for c in bundle['annotations']['cases']),
        'provider_http':0,'independent_blind_evaluation':False,'product_accuracy_claim':False,'local_full_executed':False}
    write(out/'report.json',result)
    return {k:v for k,v in result.items() if k!='cases'}


def select_cases(cases, split, case_ids=None):
    available=[c for c in cases if c['split']==split]
    if case_ids is None:return available
    if not case_ids or len(set(case_ids))!=len(case_ids) or set(case_ids)-{c['id'] for c in available}:
        raise ValueError('case_selection_must_be_unique_ids_within_split')
    return [c for c in available if c['id'] in case_ids]


def command_exit_code(result):
    if 'engineering_passed' not in result:return 0
    return 0 if result['total']>0 and result['engineering_passed']==result['total'] else 1


def audit(bundle_path, runs, output):
    """只读审计保存的真实观察，另存新报告；不覆盖原报告或伪称重新执行。"""
    bundle=read(bundle_path); validate_inputs(bundle['annotations'],bundle['inputs'])
    from app.runtime import source_identity
    if source_identity()!=bundle['application_source_identity']:raise ValueError('application_changed_before_audit')
    out=new_output(output); cases={c['id']:c for c in bundle['annotations']['cases']}; results=[]; hashes={};seen=set()
    for directory in runs:
        report=read(Path(directory)/'report.json')
        if (report['input_hash']!=bundle['input_hash'] or report['runner_sha256']!=bundle['runner_sha256']
                or report['application_source_identity']!=bundle['application_source_identity']):
            raise ValueError('observation_run_identity_mismatch')
        hashes[str(Path(directory)/'report.json')]=sha256((Path(directory)/'report.json').read_bytes()).hexdigest()
        for entry in report['cases']:
            identifier=entry['id'];path=Path(directory)/identifier/'observed.json';observed=read(path)
            if identifier in seen or observed['question']!=cases[identifier]['question'] or observed['split']!=cases[identifier]['split']:
                raise ValueError('observation_case_identity_mismatch')
            seen.add(identifier); hashes[str(path)]=sha256(path.read_bytes()).hexdigest()
            result=score(cases[identifier],observed);results.append(result)
    dimensions={}
    for name in results[0]['dimensions'] if results else []:
        values=[r['dimensions'][name] for r in results]
        dimensions[name]={'passed':sum(v is True for v in values),'failed':sum(v is False for v in values),
                          'not_applicable_or_unmeasured':sum(v is None for v in values)}
    result={'version':VERSION,'mode':'read_only_additional_audit_of_saved_observations','input_hash':bundle['input_hash'],
        'annotation_sha256':bundle['annotation_sha256'],'capture_runner_sha256':bundle['runner_sha256'],
        'audit_runner_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),'application_source_identity':source_identity(),
        'input_artifact_sha256':hashes,'cases':results,'dimensions':dimensions,
        'total':len(results),'engineering_passed':sum(r['engineering_passed'] for r in results),
        'provider_http':0,'capture_rerun':False,'independent_blind_evaluation':False,'product_accuracy_claim':False}
    result['full_responsibility_acceptance_passed']=False
    result['acceptance_limits']=['供应商与业务工具为桩，不能认证真实模型语义或真实工具接入。',
        '未实现逐项字段断言的对象语义记未测；无该断言的入口／阶段项目记不适用，不以默认True计通过。',
        '候选保留为实施者可见的12题，不是独立盲测或足量泛化认证。']
    write(out/'report.json',result)
    return {k:v for k,v in result.items() if k not in ('cases','input_artifact_sha256')}


def main():
    parser=argparse.ArgumentParser(description=__doc__); sub=parser.add_subparsers(dest='command',required=True)
    f=sub.add_parser('freeze');f.add_argument('--annotations',type=Path,required=True);f.add_argument('--seal',type=Path,required=True);f.add_argument('--inputs',type=Path,required=True);f.add_argument('--output',type=Path,required=True)
    r=sub.add_parser('run');r.add_argument('--bundle',type=Path,required=True);r.add_argument('--split',choices=['development','calibration','holdout'],required=True);r.add_argument('--output',type=Path,required=True);r.add_argument('--case-id',action='append')
    a=sub.add_parser('audit');a.add_argument('--bundle',type=Path,required=True);a.add_argument('--runs',type=Path,nargs='+',required=True);a.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    # 导入应用入口前明确隔离目录；不从正式.env初始化默认应用。
    os.environ['DOCQA_DATA_DIR']=str(args.output.resolve())
    result=(freeze(args.annotations,args.seal,args.inputs,args.output) if args.command=='freeze' else
        run(args.bundle,args.split,args.output,args.case_id) if args.command=='run' else audit(args.bundle,args.runs,args.output))
    print(json.dumps(result,ensure_ascii=False))
    return command_exit_code(result)


if __name__=='__main__':raise SystemExit(main())
