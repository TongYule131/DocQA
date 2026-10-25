"""复用一次查询向量的语义候选；示例仅在管理员显式准备时向量化。"""
from hashlib import sha256
import json
import math

from fastapi import HTTPException

from app.embedding import normalize_vector,EmbeddingError
from app.repository import now_iso
from app.retrieval_channels import fingerprint

VERSION='semantic-intent-v1'


def configuration(rows, signature):
    return fingerprint([VERSION,signature,[{k:r.get(k) for k in ('id','parent_id','revision','enabled','base_id','kind','examples','tool_id')} for r in rows]])


class SemanticIntents:
    def __init__(self,repository):self.repository=repository

    def plan(self,rows,embedding,*,max_requests=4):
        work=[]
        with self.repository.connect() as db:
            for row in rows:
                if not row['enabled'] or row['kind']=='SYSTEM':continue
                for example in row['examples']:
                    digest=sha256(example.encode()).hexdigest()
                    if not db.execute('SELECT 1 FROM intent_semantic_examples WHERE node_id=? AND revision=? AND example_hash=? AND provider_signature=?',
                        (row['id'],row['revision'],digest,embedding.signature)).fetchone():
                        work.append({'node_id':row['id'],'revision':row['revision'],'example_hash':digest,'text':example})
        batches=math.ceil(len(work)/embedding.settings.embedding_batch_size)
        if type(max_requests) is not int or not 1<=max_requests<=20 or batches>max_requests:
            raise HTTPException(422,f'需要{batches}次向量请求，超过本次上限；未外调')
        return {'work':work,'expected_requests':batches,'semantic_configuration':configuration(rows,embedding.signature)}

    def execute(self,worker,job,payload):
        from app.intent_management import IntentManagement
        from app.knowledge_tasks import TaskStopped
        if configuration(IntentManagement(self.repository).list(),worker.embedding.signature)!=payload['semantic_configuration']:
            raise TaskStopped('semantic_configuration_changed','意图示例已变化，未继续准备')
        work=payload['semantic_work'];size=worker.settings.embedding_batch_size
        for offset in range(0,len(work),size):
            batch=work[offset:offset+size]
            worker.check(job)
            with self.repository.connect() as db:
                cached=[db.execute('SELECT vector_json FROM intent_semantic_examples WHERE node_id=? AND revision=? AND example_hash=? AND provider_signature=?',
                    (r['node_id'],r['revision'],r['example_hash'],worker.embedding.signature)).fetchone() for r in batch]
            if all(cached):continue
            def call():return worker.embedding.embed([r['text'] for r in batch]),getattr(worker.embedding,'last_call_metadata',None)
            def validate(value):
                if not isinstance(value,list) or len(value)!=len(batch):raise ValueError('semantic_example_shape')
                return [normalize_vector(v) for v in value]
            vectors=worker.tasks.call(job,'index_embedding',call,checkpoint='semantic_batch_'+fingerprint(batch),validate=validate)
            with self.repository.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for entry,vector in zip(batch,vectors):
                    node=db.execute('SELECT enabled,revision FROM intent_nodes WHERE id=?',(entry['node_id'],)).fetchone()
                    if not node or not node['enabled'] or node['revision']!=entry['revision']:raise TaskStopped('semantic_configuration_changed','示例配置已变化，未发布缓存')
                    db.execute('INSERT OR REPLACE INTO intent_semantic_examples VALUES (?,?,?,?,?)',
                        (entry['node_id'],entry['revision'],entry['example_hash'],worker.embedding.signature,json.dumps(vector)))
        return {'prepared_examples':len(work),'calibration_required':True,'provider_calls_used':worker.tasks.row(job['id'])['requests_used']}

    def prepare(self,rows,embedding,*,max_requests=4):
        if type(max_requests) is not int or not 1<=max_requests<=20:raise HTTPException(422,'示例准备请求上限须为1～20')
        work=[]
        with self.repository.connect() as db:
            for row in rows:
                if not row['enabled'] or row['kind']=='SYSTEM':continue
                for example in row['examples']:
                    key=sha256(example.encode()).hexdigest()
                    if not db.execute('SELECT 1 FROM intent_semantic_examples WHERE node_id=? AND revision=? AND example_hash=? AND provider_signature=?',
                        (row['id'],row['revision'],key,embedding.signature)).fetchone():work.append((row,key,example))
        batches=math.ceil(len(work)/embedding.settings.embedding_batch_size)
        if batches>max_requests:raise HTTPException(422,f'需要{batches}次向量请求，超过本次上限，未发送')
        if work:
            vectors=embedding.embed([entry[2] for entry in work])
            if len(vectors)!=len(work):raise ValueError('semantic_example_shape')
            with self.repository.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for (row,key,_),vector in zip(work,vectors):
                    current=db.execute('SELECT revision,enabled FROM intent_nodes WHERE id=?',(row['id'],)).fetchone()
                    if not current or not current['enabled'] or current['revision']!=row['revision']:raise HTTPException(409,'示例准备期间节点已变化，未发布索引')
                    db.execute('INSERT OR REPLACE INTO intent_semantic_examples VALUES (?,?,?,?,?)',
                        (row['id'],row['revision'],key,embedding.signature,json.dumps(normalize_vector(vector))))
        return {'prepared_examples':len(work),'expected_http_requests':batches,'provider_signature':embedding.signature,
                'enabled_for_routing':False,'next_action':'使用独立校准集校准后启用'}

    def candidates(self,rows,query_vector,signature):
        q=normalize_vector(query_vector);result=[]
        with self.repository.connect() as db:
            for row in rows:
                if row['kind']=='SYSTEM':continue
                expected={sha256(s.encode()).hexdigest() for s in row['examples']}
                stored=list(db.execute('SELECT example_hash,vector_json FROM intent_semantic_examples WHERE node_id=? AND revision=? AND provider_signature=?',
                    (row['id'],row['revision'],signature)))
                if not expected or {r['example_hash'] for r in stored}!=expected:continue
                values=[normalize_vector(json.loads(r['vector_json'])) for r in stored]
                if any(len(v)!=len(q) for v in values):continue
                score=max(sum(a*b for a,b in zip(q,v)) for v in values)
                result.append({'code':row['code'],'name':row['name'],'kind':row['kind'],'method':'semantic',
                    'score':round(max(-1.0,min(1.0,score)),8),'node_id':row['id']})
        return sorted(result,key=lambda r:(-r['score'],r['code']))

    def policy(self,rows,signature):
        with self.repository.connect() as db:row=db.execute('SELECT * FROM intent_semantic_policy WHERE id=1').fetchone()
        if not row or row['provider_signature']!=signature or row['configuration']!=configuration(rows,signature):return None
        return dict(row)

    def calibrate(self,rows,signature,cases,*,threshold,margin):
        if (type(threshold) not in (int,float) or not 0<=threshold<=1 or type(margin) not in (int,float) or not 0<=margin<=1
            or not isinstance(cases,list) or len(cases)<6):raise HTTPException(422,'需要至少6条有标签向量及有效门槛')
        outputs=[]
        if any(not isinstance(c,dict) or not isinstance(c.get('id'),str) or not c['id']
            or not isinstance(c.get('expected_code'),str) or not isinstance(c.get('document_group'),str) or not c['document_group']
            or c.get('scope','knowledge') not in ('knowledge','document') or c.get('base_id') is not None and not isinstance(c['base_id'],str) for c in cases):
            raise HTTPException(422,'校准须包含有效题号、来源分组、范围和预期标签')
        if (len({c.get('id') for c in cases})!=len(cases) or not any(c.get('expected_code')=='knowledge-default' for c in cases)
            or not any(c.get('expected_code')!='knowledge-default' for c in cases)):
            raise HTTPException(422,'校准须包含唯一题号、正例和独立回退负例')
        # 校准入口只读取保存向量，不把测试台变成收费接口。
        for case in cases:
            if not all(k in case for k in ('id','expected_code','query_vector','base_id','document_group')):raise HTTPException(422,'校准标签不完整')
            from app.intent_routing import eligible_rows
            eligible=eligible_rows(rows,case['base_id'],case.get('scope','knowledge'))
            if case['expected_code']!='knowledge-default' and case['expected_code'] not in {r['code'] for r in eligible if r['kind']!='SYSTEM'}:
                raise HTTPException(422,'预期节点不在本题受控候选范围')
            try:ranked=self.candidates(eligible,case['query_vector'],signature)
            except (ValueError,TypeError,EmbeddingError):raise HTTPException(422,'校准向量形状或维度无效') from None
            selected=ranked[0]['code'] if ranked and ranked[0]['score']>=threshold and (len(ranked)<2 or ranked[0]['score']-ranked[1]['score']>=margin) else 'knowledge-default'
            outputs.append({'id':case['id'],'expected':case['expected_code'],'actual':selected,'passed':selected==case['expected_code']})
        report={'version':VERSION,'cases':outputs,'correct':sum(r['passed'] for r in outputs),'total':len(outputs),
            'independent_human_certification':False,'provider_calls':0}
        if report['correct']/report['total']<0.95:raise HTTPException(422,{'message':'校准集未达到规定95%目标，未启用','report':report})
        with self.repository.connect() as db:
            db.execute('INSERT OR REPLACE INTO intent_semantic_policy VALUES (1,?,?,?,?,?,?,?)',
                (threshold,margin,fingerprint(cases),signature,json.dumps(report),configuration(rows,signature),now_iso()))
        return report
