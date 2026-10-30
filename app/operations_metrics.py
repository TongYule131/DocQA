"""后台仅投影计数和耗时；不返回问题、原文、回答或调用响应。"""
from datetime import datetime,timezone
import json,math


def distribution(values):
    values=sorted(float(v) for v in values if v is not None and v>=0)
    return {'count':len(values),'p50_ms':round(values[math.ceil(len(values)*.5)-1],2) if values else None,
        'p95_ms':round(values[math.ceil(len(values)*.95)-1],2) if values else None}


def project(db,day):
    calls=db.execute("SELECT elapsed_ms,status FROM kb_task_calls WHERE started_at>=? AND delivery_status!='not_sent' ORDER BY started_at DESC LIMIT 1000",(day,)).fetchall()
    total=[r[0] for r in db.execute("SELECT (julianday(finished_at)-julianday(started_at))*86400000 FROM kb_tasks WHERE started_at>=? AND finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1000",(day,))]
    first=[r[0] for r in db.execute("SELECT (julianday(MIN(e.created_at))-julianday(t.started_at))*86400000 FROM answer_stream_events e JOIN kb_tasks t ON t.id=e.kb_task_id WHERE e.kind='block' AND COALESCE(json_extract(e.payload_json,'$.validation_scope'),'')='single_fact' AND t.started_at>=? GROUP BY t.id ORDER BY t.started_at DESC LIMIT 1000",(day,))]
    contexts=[r[0] for r in db.execute("SELECT json_extract(payload_json,'$.context_chars') FROM kb_task_checkpoints WHERE name LIKE 'diagnostic_retrieval_attempt_%' AND created_at>=? ORDER BY created_at DESC LIMIT 1000",(day,))]
    contexts=[v for v in contexts if type(v) in (float,int)]
    return {'window_start':day,'timezone':'UTC','scope':'knowledge_tasks','sample_limit':1000,
        'total':distribution(total),'first_validated_block':distribution(first),'external_call':distribution([r['elapsed_ms'] for r in calls]),
        'external_failures':sum(r['status']=='failed' for r in calls),'external_uncertain':sum(r['status']=='uncertain' for r in calls),
        'context':{'count':len(contexts),'max_chars':max(contexts) if contexts else None,'mean_chars':round(sum(contexts)/len(contexts),2) if contexts else None}}


def backup_status(data_dir):
    try:
        value=json.loads((data_dir/'.local-service/last-backup.json').read_text(encoding='utf-8'))
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(value['completed_at'])).total_seconds()
        if age< -30 or value.get('status')!='completed':return {'status':'unknown'}
        return {k:value[k] for k in ('status','completed_at','schema_version','file_count','manifest_sha256')}|{'age_seconds':round(age)}
    except (OSError,ValueError,KeyError,TypeError):return {'status':'unknown'}
