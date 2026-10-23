"""链路只展示运行元数据；后台管理员不因诊断获得客户问题、原文或回答。"""
import math
import json
from app.repository import parse_iso


def milliseconds(start,end):
    a,b=parse_iso(start),parse_iso(end)
    return max(0,round((b-a).total_seconds()*1000)) if a and b else None


class KnowledgeTraces:
    def __init__(self,repository):self.repository=repository

    def records(self):
        result=[]
        with self.repository.connect() as db:
            for row in db.execute('SELECT j.*,u.username FROM ingestion_jobs j LEFT JOIN product_users u ON u.id=j.owner_user_id ORDER BY j.created_at DESC LIMIT 500'):
                result.append({'id':row['id'],'name':'knowledge-ingestion','source':'ingestion','status':row['status'],'owner':row['username'] or '本地用户',
                    'created_at':row['created_at'],'started_at':row['created_at'],'finished_at':row['updated_at'] if row['status'] not in ('queued','running','review') else None,
                    'elapsed_ms':milliseconds(row['created_at'],row['updated_at']) if row['status'] not in ('queued','running','review') else None,
                    'requests_used':None,'error_code':row['error_code'],'stage':row['stage']})
            for table,kind in [('kb_tasks','knowledge'),('question_jobs','question')]:
                for row in db.execute(f'SELECT j.*,u.username FROM {table} j LEFT JOIN product_users u ON u.id=j.owner_user_id ORDER BY j.created_at DESC LIMIT 500'):
                    result.append({'id':row['id'],'name':('knowledge-index' if row['kind']=='index' else 'knowledge-chat') if table=='kb_tasks' else 'document-chat',
                        'source':kind,'status':row['status'],'owner':row['username'] or '本地用户',
                        'created_at':row['created_at'],'started_at':row['started_at'],'finished_at':row['finished_at'],
                        'elapsed_ms':milliseconds(row['started_at'],row['finished_at']),'requests_used':row['requests_used'],
                        'error_code':row['error_code'],'stage':row['stage']})
            for row in db.execute('SELECT * FROM parse_tasks ORDER BY created_at DESC LIMIT 500'):
                result.append({'id':row['id'],'name':'document-parse','source':'parse','status':row['status'],
                    'owner':'管理员 / 文档处理','created_at':row['created_at'],'started_at':row['started_at'],'finished_at':row['finished_at'],
                    'elapsed_ms':milliseconds(row['started_at'],row['finished_at']),'requests_used':None,'error_code':row['error_code'],'stage':row['stage']})
        return sorted(result,key=lambda r:r['created_at'],reverse=True)

    def list(self,query='',status='',page=1):
        rows=[r for r in self.records() if (not query or query.casefold() in (r['id']+' '+r['name']+' '+r['owner']).casefold()) and (not status or r['status']==status)]
        times=sorted(r['elapsed_ms'] for r in rows if r['elapsed_ms'] is not None)
        completed=[r for r in rows if r['status'] not in ('queued','running','review')]
        stats={'succeeded':sum(r['status']=='succeeded' for r in rows),'failed':sum(r['status'] in ('failed','needs_attention') for r in rows),
            'running':sum(r['status'] in ('queued','running','review') for r in rows),
            'success_rate':round(100*sum(r['status']=='succeeded' for r in completed)/len(completed),1) if completed else None,
            'average_ms':round(sum(times)/len(times)) if times else None,'p95_ms':times[max(0,math.ceil(.95*len(times))-1)] if times else None}
        return {'items':rows[(page-1)*20:page*20],'total':len(rows),'page':page,'stats':stats,'window':'解析、索引与问答各最新500条任务；筛选后的统计'}

    def detail(self,identifier):
        record=next((r for r in self.records() if r['id']==identifier),None)
        if not record:return None
        children=[]
        with self.repository.connect() as db:
            if record['source']=='ingestion':
                job=db.execute('SELECT parse_task_id,index_task_id FROM ingestion_jobs WHERE id=?',(identifier,)).fetchone()
                children=[i for i in tuple(job) if i];spans=[];calls=[]
            elif record['source']=='knowledge':
                spans=[dict(r) for r in db.execute('SELECT * FROM kb_trace_spans WHERE task_id=? ORDER BY started_at,rowid',(identifier,))]
                calls=[dict(r) for r in db.execute('SELECT sequence_no,role,status,error_code,started_at,finished_at,elapsed_ms FROM kb_task_calls WHERE task_id=? ORDER BY sequence_no',(identifier,))]
            elif record['source']=='parse':
                calls=[]
                spans=[dict(id=r['id'],parent_id=None,name='解析尝试 '+str(r['attempt_no']),kind='PARSE',status=r['status'],started_at=r['started_at'],finished_at=r['finished_at'],elapsed_ms=milliseconds(r['started_at'],r['finished_at']),error_code=r['error_code']) for r in db.execute('SELECT * FROM parse_attempts WHERE task_id=? ORDER BY attempt_no',(identifier,))]
            else:
                calls=[dict(r) for r in db.execute('SELECT sequence_no,role,status,error_code,started_at,settled_at AS finished_at,elapsed_ms FROM question_calls WHERE job_id=? ORDER BY sequence_no',(identifier,))]
                spans=[dict(id='legacy-call-'+str(r['sequence_no']),parent_id=None,name='查询向量' if r['role']=='embedding' else '回答生成',kind=r['role'].upper(),**r) for r in calls]
        for child in children:
            detail=self.detail(child)
            if detail:
                spans.extend({**s,'id':child+'/'+s['id'],'parent_id':child+'/'+s['parent_id'] if s.get('parent_id') else None} for s in detail['spans'])
                calls.extend({**c,'task_id':child} for c in detail['calls'])
        intent=None;diagnostics=[]
        if record['source']=='knowledge':
            with self.repository.connect() as db:
                rows=db.execute("SELECT name,payload_json FROM kb_task_checkpoints WHERE task_id=? AND name LIKE 'diagnostic_%' ORDER BY created_at",(identifier,)).fetchall()
            for row in rows:
                value=json.loads(row['payload_json']);attempt=row['name'].rsplit('_',1)[-1]
                if row['name'].startswith('diagnostic_validation_'):
                    location=value.get('location',{})
                    diagnostics.append(dict(kind='validation',attempt=attempt,reason=value.get('reason'),
                        location={k:location[k] for k in ('category','ordinal','fact_index','reference_count','numeric_count') if k in location}))
                elif row['name'].startswith('diagnostic_retrieval_'):
                    diagnostics.append(dict(kind='retrieval',attempt=attempt,
                        **{k:value.get(k) for k in ('admitted_count','context_chars','truncated','drop_count')}))
        if record['source'] in ('knowledge','question'):
            with self.repository.connect() as db:
                source=db.execute('SELECT '+('input_json FROM kb_tasks' if record['source']=='knowledge' else 'snapshot_json FROM question_jobs')+' WHERE id=?',(identifier,)).fetchone()
            payload=json.loads(source[0])
            decision=payload.get('intent_decision') if record['source']=='knowledge' else payload.get('intent_resolution',{}).get('decision')
            if decision:
                from app.intent_routing import public_decision
                intent=public_decision(decision)
        for span in spans:span['offset_ms']=milliseconds(record['started_at'],span['started_at'])
        return {**record,'spans':spans,'calls':calls,'children':children,'intent':intent,'diagnostics':diagnostics,'content_policy':'仅含运行元数据；不展示用户问题、提示词、文档正文或原始响应。',
                'timing_note':'旧任务只展示实际记录的外调节点；未记录的阶段不补造耗时。'}
