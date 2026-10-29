"""N03隔离工程链：实际上传/解析/索引/生效/问答，供应商为有界桩，禁止外部网络。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack,closing
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.evaluate_intent_responsibilities import FrozenModel,OfflineEmbedding,LocalRuntime,no_external_network
RUNNER_SOURCE_SHA256=sha256(Path(__file__).read_bytes()).hexdigest()

PASSWORD='Synthetic-N03-only-2026'
RESPONSES={
    'MD17的处理要求是什么？':{'text':'MD17要求停止后续调用，并保留已发送费用。','quote_texts':['| MD17 | 停止后续调用 | 保留已发送费用 |'],'context_kind':'subject'},
    'PARSE_MODE的示例字面值是什么？':{'text':'示例中的PARSE_MODE值为preview_only。','quote_texts':['PARSE_MODE = "preview_only"']},
    '续借生效的前提是什么？':{'text':'续借仅在登记完成并经负责人确认后生效。','quote_texts':['续借仅在登记完成并经负责人确认后生效。']},
}


def digest(value):return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def read(path):return json.loads(Path(path).read_text('utf-8'))
def save(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)


class ProtocolModel:
    def __init__(self):self.calls=[];self.outputs=[]
    def generate(self,system,user):
        value=json.loads(user);response=RESPONSES[value['question']]
        raw=FrozenModel({'response':{**response,'claim_type':'direct','missing_part_ids':[]}}).generate(system,user)
        if response.get('context_kind') and 'references' in value:
            # 标识含数字时仍按现有严格协议带本条真实来源限定，不能放松数字守卫。
            payload=json.loads(raw);fact=payload['conclusion'][0];unit=fact['evidence'][0]
            reference=next(r for r in value['references'] if r['ref']==unit['ref'])
            part=reference['quote_units'][str(unit['unit'])][0]
            ref,span,first,last=part if len(part)==4 else (unit['ref'],*part)
            fact['context']=[{'kind':response['context_kind'],'ref':ref,'span':span,'segment':first}]
            raw=json.dumps(payload,ensure_ascii=False)
        self.calls.append({'system':system,'user':user});self.outputs.append(raw);return raw
    def generate_stream(self,system,user,on_text,check_active):
        raw=self.generate(system,user)
        for offset in range(0,len(raw),61):check_active();on_text(raw[offset:offset+61])
        return raw


class Runtime:
    def __init__(self,directory):self.directory=directory;self.stack=ExitStack();self.entries=[]
    def __enter__(self):
        self.stack.enter_context(no_external_network())
        from app.config import Settings
        from app.repository import Repository
        from app.product_access import ProductAccess
        from app.vector_index import DocumentIndex
        from fastapi.testclient import TestClient
        self.settings=Settings(data_dir=self.directory,embedding_api_key='offline-only',deepseek_api_key='offline-only',
            team_mode=True,session_cookie_secure=False,question_max_requests=8)
        self.repository=Repository(self.directory/'docqa.db');self.repository.settings=self.settings;self.repository.initialize()
        self.embedding=OfflineEmbedding(self.settings);self.model=ProtocolModel()
        self.stack.enter_context(patch.object(Settings,'from_env',return_value=self.settings))
        module=importlib.import_module('app.main')
        self.stack.enter_context(patch.object(module,'APIEmbedding',lambda _:self.embedding))
        self.stack.enter_context(patch.object(module,'DeepSeekModel',lambda _:self.model))
        access=ProductAccess(self.repository,self.settings,DocumentIndex(self.repository,self.embedding))
        with self.repository.connect() as db:existing=db.execute("SELECT 1 FROM product_users WHERE username='n03-admin'").fetchone()
        if not existing:
            access.create_user('n03-admin','N03合成管理员',PASSWORD,'admin');access.create_user('n03-customer','N03合成客户',PASSWORD,'customer')
        app=module.create_app(self.settings)
        self.clients={name:self.stack.enter_context(TestClient(app)) for name in ('n03-admin','n03-customer')}
        for name in self.clients:
            response=self.http(name,'POST','/api/auth/login',{'username':name,'password':PASSWORD})
            assert response.status_code==200;self.clients[name].headers['X-CSRF-Token']=response.json()['csrf_token']
        with self.repository.connect() as db:self.customer_id=db.execute("SELECT id FROM product_users WHERE username='n03-customer'").fetchone()[0]
        return self
    def __exit__(self,*args):return self.stack.__exit__(*args)
    def http(self,actor,method,path,body=None,**kwargs):
        response=self.clients[actor].request(method,path,**({'json':body} if body is not None else {}),**kwargs)
        self.entries.append({'actor':actor,'method':method,'path':path,'http_status':response.status_code,
            'request':None if path=='/api/auth/login' else body,'response_sha256':sha256(response.content).hexdigest()})
        return response
    def parse(self,document,key,force):
        from app.parse_worker import ParseWorker
        response=self.http('n03-admin','POST','/api/documents/'+document+'/parse',{'idempotency_key':key,'force':force})
        assert response.status_code==202,response.text
        task=response.json()['task']['id'];before=len(self.embedding.calls),len(self.model.calls)
        worker=ParseWorker(self.settings,self.repository);worker.runtime=LocalRuntime();assert worker.run_task(task)
        row=self.repository.get_task(task);assert row.status=='succeeded',row
        assert before==(len(self.embedding.calls),len(self.model.calls))
        return self.repository.get(document).active_parse_version_id
    def index(self,document,key):
        from app.knowledge_worker import KnowledgeWorker
        response=self.http('n03-admin','POST','/api/admin/knowledge/documents/'+document+'/index-tasks',{'request_key':key,'rebuild':True})
        assert response.status_code==202,response.text;task=response.json()['id']
        worker=KnowledgeWorker(self.settings,self.repository,LocalRuntime(),embedding=self.embedding,model=self.model)
        worker.run_once(task);row=worker.tasks.row(task);assert row['status']=='succeeded',row
        return self.repository.get(document).active_index_id
    def ask(self,base,document,key):
        from app.knowledge_worker import KnowledgeWorker
        results=[]
        for number,question in enumerate(RESPONSES):
            response=self.http('n03-customer','POST','/api/chat/conversations',{'base_id':base,'request_key':key+'-conv-'+str(number),'document_ids':[document]})
            assert response.status_code==201,response.text;conv=response.json()['id']
            body={'question':question,'request_key':key+'-ask-'+str(number)}
            response=self.http('n03-customer','POST','/api/chat/conversations/'+conv+'/turns',body)
            assert response.status_code==202,response.text;task=response.json()['task_id']
            worker=KnowledgeWorker(self.settings,self.repository,LocalRuntime(),embedding=self.embedding,model=self.model)
            worker.run_once(task);row=worker.tasks.row(task);assert row['status']=='succeeded',row
            detail=self.http('n03-customer','GET','/api/kb-tasks/'+task).json();assert detail['result']['status']=='answered'
            payload=json.loads(row['input_json']);snapshot=payload['documents'][0]['snapshot'];active=self.repository.get(document)
            assert snapshot['parse_version_id']==active.active_parse_version_id and snapshot['index_id']==active.active_index_id
            assert all(c['document_id']==document and c['parse_version_id']==active.active_parse_version_id for c in detail['result']['citations'])
            from app.answer_stream_store import AnswerStreamStore
            events=AnswerStreamStore(self.repository).read('knowledge',task)
            assert any(e['kind']=='block' for e in events)
            before=len(self.embedding.calls),len(self.model.calls)
            again=self.http('n03-customer','POST','/api/chat/conversations/'+conv+'/turns',body)
            assert again.json()['task_id']==task
            assert self.http('n03-customer','GET','/api/chat/conversations/'+conv).status_code==200
            assert self.http('n03-customer','GET','/api/kb-tasks/'+task+'/export').status_code==200
            assert before==(len(self.embedding.calls),len(self.model.calls))
            results.append({'question':question,'task_id':task,'result':detail['result'],'task_row_sha256':digest(row),
                'snapshot':snapshot,'stream_events_sha256':digest(events),'stream_blocks':sum(e['kind']=='block' for e in events)})
        return results


def source_text(sentinel):
    lines=['# 客户端 C#','','## 事件处理 ###','','| 事件 | 处理 | 后续 |','|---|---|---|',
        '| MD17 | 停止后续调用 | 保留已发送费用 |','| MD18 | 核对来源版本 | 不覆盖旧结果 |',
        r'| `a|b` | c\|d | 保留管道字面量 |','','## 仅供展示的代码','','```python',
        'PARSE_MODE = "preview_only"','from pathlib import Path','Path('+repr(str(sentinel))+').write_text("must-not-run")']
    lines+=['    sample_'+str(i)+' = "preserve indentation"' for i in range(45)]
    lines+=['# '+('完整代码行边界。'*34)]
    lines+=['# 代码内部不是新章节','```','','## 续借范围','','续借仅在登记完成并经负责人确认后生效。',
        '链接 https://example.invalid/n03 和 <script>synthetic()</script> 都只作为资料。','']
    return '\r\n'.join(lines)


def retention(runtime,version,index,tasks):
    result={}
    with runtime.repository.connect() as db:
        for table,column,value in [('parse_versions','id',version),('blocks','parse_version_id',version),('chunks','parse_version_id',version),
            ('sources','parse_version_id',version),('quality_warnings','parse_version_id',version),('embedding_vectors_v2','index_id',index)]:
            result[table]=digest([dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE '+column+'=? ORDER BY rowid',(value,))])
        row=dict(db.execute('SELECT * FROM embedding_indexes WHERE id=?',(index,)).fetchone());row.pop('is_active',None);result['index_identity']=digest(row)
        result['old_tasks']=digest([dict(db.execute('SELECT * FROM kb_tasks WHERE id=?',(task,)).fetchone()) for task in tasks])
    return result


def run(output,source=None,*,chunk_max_chars=None,probe_failed_parse=False):
    if sha256(Path(__file__).read_bytes()).hexdigest()!=RUNNER_SOURCE_SHA256:raise ValueError('runner_changed_since_import')
    directory=Path(output).resolve();directory.mkdir(parents=True,exist_ok=False);os.environ['DOCQA_DATA_DIR']=str(directory)
    previous=read(Path(source)/'report.json') if source else None
    if previous:
        source=Path(source).resolve()
        assert previous['passed'] and sha256((source/'source.md').read_bytes()).hexdigest()==previous['original_sha256']
        with closing(sqlite3.connect((source/'docqa.db').as_uri()+'?mode=ro',uri=True)) as db,closing(sqlite3.connect(directory/'docqa.db')) as target:db.backup(target)
        for folder in ('uploads','parse-results'):
            if (source/folder).exists():shutil.copytree(source/folder,directory/folder)
        (directory/'source.md').write_bytes((source/'source.md').read_bytes())
    else:(directory/'source.md').write_bytes(source_text(directory/'must-not-run.txt').encode())
    from app.runtime import source_identity
    identity=source_identity()
    (directory/'capture-runner.py').write_bytes(Path(__file__).read_bytes())
    save(directory/'start.json',{'application_source_identity':identity,'runner_sha256':RUNNER_SOURCE_SHA256,'provider_http':0})
    runtime=Runtime(directory)
    try:
        with runtime as r:
            raw=(directory/'source.md').read_bytes()
            if previous:
                base,document=previous['base_id'],previous['document_id'];prior=retention(r,previous['parse_version_id'],previous['index_id'],[v['task_id'] for v in previous['answers']])
                assert prior==previous['retention']
                if chunk_max_chars is not None:
                    if type(chunk_max_chars) is not int or not 200<=chunk_max_chars<=16000:raise ValueError('invalid_isolated_chunk_fixture')
                    # 仅隔离测试输入设置；不声称通过不存在的配置编辑UI，也不改原件。
                    with r.repository.connect() as db:db.execute('UPDATE knowledge_bases SET chunk_max_chars=?,chunk_overlap_chars=? WHERE id=?',
                        (chunk_max_chars,min(80,chunk_max_chars//10),base))
            else:
                response=r.http('n03-admin','POST','/api/knowledge-bases',{'name':'N03离线结构资料','chunk_max_chars':800,'chunk_overlap_chars':80,'activation_policy':'review'})
                assert response.status_code==201,response.text;base=response.json()['id']
                response=r.http('n03-admin','POST','/api/admin/knowledge/bases/'+base+'/documents',files={'file':('n03-structure.md',raw,'text/markdown')},headers={'Idempotency-Key':'n03-source'})
                assert response.status_code==201,response.text;document=response.json()['document']['id']
            version=r.parse(document,'new-policy' if previous else 'old-policy',bool(previous))
            blocks=r.repository.blocks(version);chunks=r.repository.chunks(document,version)
            assert any(b.block_type=='markdown_table' for b in blocks) and any(c.chunk_type=='code' for c in chunks)
            assert len([c for c in chunks if c.chunk_type=='table_rows'])==3
            text=raw.decode('utf-8')
            for block in blocks:
                assert len(block.sources)==1 and text[block.sources[0].char_start:block.sources[0].char_end]==block.text
                assert block.sources[0].section_path==block.heading_path
            for chunk in chunks:
                if chunk.chunk_type in ('code','table_rows'):
                    restored=''.join(text[s.char_start:s.char_end] for s in chunk.sources)
                    assert (restored if chunk.chunk_type=='code' else restored.strip())==chunk.text
            code=[c.text for c in chunks if c.chunk_type=='code'];assert len(code)>1 and ''.join(code)==next(b.text for b in blocks if b.block_type=='code')
            index=r.index(document,'new-index' if previous else 'old-index')
            response=r.http('n03-admin','POST','/api/knowledge-bases/'+base+'/publish',{'expected_revision':1 if previous else 0})
            assert response.status_code==200,response.text
            if not previous:assert r.http('n03-admin','PUT','/api/knowledge-bases/'+base+'/members/'+r.customer_id,{'enabled':True}).status_code==200
            answers=r.ask(base,document,'new' if previous else 'old')
            if previous:
                assert retention(r,previous['parse_version_id'],previous['index_id'],[a['task_id'] for a in previous['answers']])==prior
                for answer in previous['answers']:
                    current=r.http('n03-customer','GET','/api/kb-tasks/'+answer['task_id']);assert current.status_code==200,current.text
                    assert current.json()['result']==answer['result']
                assert r.http('n03-admin','GET','/api/documents/'+document+'/chunks?version_id='+previous['parse_version_id']).status_code==200
                historical=r.http('n03-customer','GET','/api/documents/'+document+'/chunks?version_id='+previous['parse_version_id'])
                assert historical.status_code==200,'自己的库级旧回答应能打开原版本来源：'+historical.text
                for file in (source/'parse-results'/previous['parse_version_id']).rglob('*'):
                    if file.is_file():assert (directory/file.relative_to(source)).read_bytes()==file.read_bytes()
            original=r.http('n03-customer','GET','/api/documents/'+document+'/original');assert original.content==raw
            overflow=None
            if probe_failed_parse:
                from app.parse_worker import ParseWorker
                assert max(map(len,text.splitlines()))>200,'该隔离输入须有超过200字符的完整代码行'
                before_counts=len(r.embedding.calls),len(r.model.calls)
                with r.repository.connect() as db:old_config=dict(db.execute('SELECT chunk_max_chars,chunk_overlap_chars,active_snapshot_id FROM knowledge_bases WHERE id=?',(base,)).fetchone())
                try:
                    with r.repository.connect() as db:db.execute('UPDATE knowledge_bases SET chunk_max_chars=200,chunk_overlap_chars=20 WHERE id=?',(base,))
                    response=r.http('n03-admin','POST','/api/documents/'+document+'/parse',{'idempotency_key':'expected-overflow','force':True})
                    assert response.status_code==202
                    worker=ParseWorker(r.settings,r.repository);worker.runtime=LocalRuntime();worker.run_task(response.json()['task']['id'])
                    failed=r.repository.get_task(response.json()['task']['id']);assert failed.status=='failed' and failed.error_code=='chunking_invalid'
                    assert r.repository.get(document).active_parse_version_id==version and r.repository.get(document).active_index_id==index
                    with r.repository.connect() as db:assert db.execute('SELECT active_snapshot_id FROM knowledge_bases WHERE id=?',(base,)).fetchone()[0]==old_config['active_snapshot_id']
                    assert before_counts==(len(r.embedding.calls),len(r.model.calls))
                    overflow={'task_id':failed.id,'status':failed.status,'error_code':failed.error_code,'active_parse_preserved':True,'active_index_preserved':True,'publication_preserved':True,'provider_stub_delta':0}
                finally:
                    with r.repository.connect() as db:db.execute('UPDATE knowledge_bases SET chunk_max_chars=?,chunk_overlap_chars=? WHERE id=?',
                        (old_config['chunk_max_chars'],old_config['chunk_overlap_chars'],base))
            sentinels=[directory/'must-not-run.txt']+([Path(previous['sentinel'])] if previous else [])
            assert not any(p.exists() for p in sentinels)
            assert source_identity()==identity
            assert sha256(Path(__file__).read_bytes()).hexdigest()==RUNNER_SOURCE_SHA256
            result={'passed':True,'application_source_identity':identity,'provider_http':0,'external_network_blocked':True,'real_semantic_evaluation':False,
                'isolated_chunk_fixture_override':chunk_max_chars,
                'overflow_probe':overflow,
                'source_phase':str(source) if previous else None,'source_phase_report_sha256':sha256((source/'report.json').read_bytes()).hexdigest() if previous else None,
                'base_id':base,'document_id':document,'parse_version_id':version,'parser_version':r.repository.get_parse_version(version).parser_version,'index_id':index,
                'original_sha256':sha256(raw).hexdigest(),'sentinel':str(Path(previous['sentinel']) if previous else sentinels[0]),'sentinel_absent':True,
                'blocks':[b.model_dump() for b in blocks],'chunks':[c.model_dump() for c in chunks],'answers':answers,'old_results_unchanged':True if previous else None,
                'retention':retention(r,version,index,[a['task_id'] for a in answers]),'entries':r.entries,'embedding_stub_calls':r.embedding.calls,
                'model_stub_inputs':r.model.calls,'model_stub_outputs':r.model.outputs,'logical_stub_counts':{'embedding':len(r.embedding.calls),'generation':len(r.model.calls)}}
            save(directory/'report.json',result)
            return {k:result[k] for k in ('passed','application_source_identity','parser_version','parse_version_id','index_id','provider_http','old_results_unchanged','logical_stub_counts')}
    except Exception:
        import traceback
        (directory/'first-failure.txt').write_text(traceback.format_exc(),'utf-8');raise
    finally:runtime.stack.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path);parser.add_argument('--source',type=Path)
    parser.add_argument('--chunk-max',type=int,help='仅隔离副本的分块配置输入，不修改原件')
    parser.add_argument('--probe-failed-parse',action='store_true',help='在隔离库降低分块上限，验证失败不撤换已有版本和生效指针')
    args=parser.parse_args();print(json.dumps(run(args.output,args.source,chunk_max_chars=args.chunk_max,probe_failed_parse=args.probe_failed_parse),ensure_ascii=False))
