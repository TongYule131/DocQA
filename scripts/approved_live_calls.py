"""显式冻结阶段的唯一账本适配；每个真实HTTP先预扣，不重发未知请求。"""
from contextlib import contextmanager,closing
from contextvars import ContextVar
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
import zlib
from datetime import datetime,timezone

import httpx

from app.evaluation_budget import EvaluationStopped,_safe_metadata

CALL_PURPOSE=ContextVar('approved_evaluation_purpose',default=None)


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def hashed(value):return sha256(canonical(value).encode()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()


def decoded_body(body):
    # SDK在SSE的[DONE]后关闭响应，gzip尾部可能未读；仍保留原始字节。
    if body.startswith(b'\x1f\x8b'):
        try:return zlib.decompressobj(31).decompress(body,16*1024*1024)
        except zlib.error:return b''
    return body


def completed_sse(body):
    try:
        lines=decoded_body(body).decode('utf-8').splitlines()
        data=[line[5:].strip() for line in lines if line.startswith('data:')]
        if not data or data[-1]!='[DONE]' or data.count('[DONE]')!=1:return False
        values=[json.loads(line) for line in data[:-1]]
        finishes=[c['finish_reason'] for value in values for c in value.get('choices',[]) if c.get('finish_reason')]
        return finishes==['stop'] and any(isinstance(v.get('usage'),dict) and type(v['usage'].get('total_tokens')) is int for v in values)
    except (ValueError,UnicodeError,TypeError,KeyError):return False


@contextmanager
def purpose(name,role='generation'):
    token=CALL_PURPOSE.set((name,role))
    try:yield
    finally:CALL_PURPOSE.reset(token)


class ApprovedCalls:
    def __init__(self,path,plan,authorization,output):
        self.path=Path(path).resolve();self.plan=plan;self.authorization=authorization;self.output=Path(output).resolve()
        self.output.mkdir(parents=True,exist_ok=False)
        scope=authorization.get('scope')
        if (authorization.get('max_model_http')!=80 or plan.get('max_model_http')!=80 or
            scope not in ('N01-N02-current-run','N03-current-run') or scope!=plan.get('scope','N01-N02-current-run') or not plan.get('stage_id') or
            sum(plan['http_slots'].values())>80 or any(type(n) is not int or n<0 for n in plan['http_slots'].values())):
            raise EvaluationStopped('本次80次授权或冻结HTTP清单无效')
        self.stage=plan['stage_id'];self.start=plan['start_sequence'];self.stopped=False

    def connect(self):return sqlite3.connect(self.path,timeout=30)

    def activate(self):
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT COALESCE(MAX(sequence),0) FROM calls').fetchone()[0]
            if current!=self.plan.get('activation_sequence',self.start):raise EvaluationStopped('唯一账本已变化，先重新核对，不发送请求')
            if self.authorization.get('used_at_approval')!=self.start:raise EvaluationStopped('批准起点不匹配')
            old=db.execute('SELECT max_requests,fingerprint FROM approval WHERE id=1').fetchone()
            if list(old)!=self.plan['previous_approval']:raise EvaluationStopped('旧批准记录变化')
            db.execute('CREATE TABLE IF NOT EXISTS evaluation_stage_approvals (stage_id TEXT PRIMARY KEY,start_sequence INTEGER NOT NULL,max_stage_requests INTEGER NOT NULL,plan_sha256 TEXT NOT NULL,authorization_sha256 TEXT NOT NULL,previous_approval_json TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL)')
            if current>self.start:
                stages=self.plan.get('previous_stage_ids',[])
                if not stages:raise EvaluationStopped('续作缺少原80次授权的阶段链')
                for stage in stages:
                    prior=db.execute('SELECT start_sequence,max_stage_requests,authorization_sha256,status FROM evaluation_stage_approvals WHERE stage_id=?',(stage,)).fetchone()
                    if not prior or prior[:3]!=(self.start,80,hashed(self.authorization)) or prior[3]=='active':
                        raise EvaluationStopped('续作不是同一80次批准或前阶段尚未停止')
                if old[0]!=self.start+80:raise EvaluationStopped('续作不能扩大原80次批准')
            db.execute("INSERT INTO evaluation_stage_approvals VALUES (?,?,?,?,?,?,?,'active')",(self.stage,self.start,80,hashed(self.plan),hashed(self.authorization),canonical(list(old)),now()))
            for sequence,proof in self.plan.get('completed_stream_proofs',{}).items():
                if not self.verified_completed(int(sequence)):raise EvaluationStopped('流完成证据无效，不能续作')
                db.execute('CREATE TABLE IF NOT EXISTS evaluation_call_reconciliations (sequence INTEGER PRIMARY KEY,response_sha256 TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,stage_id TEXT NOT NULL,created_at TEXT NOT NULL)')
                db.execute('INSERT OR IGNORE INTO evaluation_call_reconciliations VALUES (?,?,?,?,?)',(int(sequence),proof['response_sha256'],proof['evidence_sha256'],self.stage,now()))
            db.execute('UPDATE approval SET max_requests=?,fingerprint=? WHERE id=1',(self.start+80,hashed(self.plan)))

    def verified_completed(self,sequence):
        proof=self.plan.get('completed_stream_proofs',{}).get(str(sequence))
        if not proof:return False
        body=Path(proof['response_file']).read_bytes()
        evidence=Path(proof['evidence_file']).read_bytes()
        return (sha256(body).hexdigest()==proof['response_sha256'] and sha256(evidence).hexdigest()==proof['evidence_sha256']
            and completed_sse(body) and json.loads(evidence).get('sequence')==sequence)

    def reserve(self,request):
        if self.stopped:raise EvaluationStopped('本轮已触发停止条件')
        current=CALL_PURPOSE.get()
        if current is None:raise EvaluationStopped('HTTP缺少冻结归属')
        name,role=current
        if name not in self.plan['http_slots'] or role not in ('index','query','generation'):
            raise EvaluationStopped('HTTP归属不在冻结清单')
        if request.method!='POST' or str(request.url) not in self.plan['allowed_urls']:
            raise EvaluationStopped('HTTP目的地址或方法不在冻结清单')
        content=request.read()
        context=self.stage+'/'+name
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            stage=db.execute('SELECT status FROM evaluation_stage_approvals WHERE stage_id=?',(self.stage,)).fetchone()
            if not stage or stage[0]!='active':raise EvaluationStopped('本阶段已持久停止或未激活')
            if db.execute('SELECT max_requests,fingerprint FROM approval WHERE id=1').fetchone()!=(self.start+80,hashed(self.plan)):
                raise EvaluationStopped('本次批准状态变化')
            rows=db.execute('SELECT sequence,context,status FROM calls WHERE sequence>? ORDER BY sequence',(self.start,)).fetchall()
            stages=[self.stage]+self.plan.get('previous_stage_ids',[])
            if any(not any(c.startswith(stage+'/') for stage in stages) for _,c,_ in rows):raise EvaluationStopped('账本出现非本阶段新增请求，暂停核对')
            if any(s=='intent' or s=='uncertain' and not self.verified_completed(seq) for seq,_,s in rows):raise EvaluationStopped('本轮存在在途或不确定HTTP，不自动重发')
            if len(rows)>=80 or sum(c.split('/',1)[-1]==name for _,c,_ in rows)>=self.plan['http_slots'][name]:
                raise EvaluationStopped('本次或逐HTTP上界已达，不发送请求')
            sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM calls').fetchone()[0]
            db.execute("INSERT INTO calls(sequence,role,context,request_sha256,status,started_at) VALUES (?,?,?,?,'intent',?)",
                (sequence,role,context,sha256(content).hexdigest(),now()))
        (self.output/(str(sequence)+'.request.json')).write_bytes(content)
        return sequence

    def settle(self,sequence,body,status_code,started,*,uncertain=False):
        metadata={}
        try:
            decoded=decoded_body(body)
            if b'data:' in decoded[:100]:
                values=[json.loads(line[5:].strip()) for line in decoded.decode('utf-8').splitlines() if line.startswith('data:') and line[5:].strip() not in ('','[DONE]')]
                for value in values:
                    if value.get('usage'):metadata['usage']=value['usage']
                    if value.get('id'):metadata['provider_request_id']=value['id']
                    if value.get('choices') and value['choices'][0].get('finish_reason'):metadata['finish_reason']=value['choices'][0]['finish_reason']
            else:
                value=json.loads(decoded);metadata={'usage':value.get('usage'),'provider_request_id':value.get('id')}
                if value.get('choices'):metadata['finish_reason']=value['choices'][0].get('finish_reason')
        except (ValueError,UnicodeError,TypeError,KeyError,IndexError):pass
        status='uncertain' if uncertain else 'succeeded' if status_code==200 else 'failed'
        if status!='succeeded':self.stopped=True
        (self.output/(str(sequence)+'.response.bin')).write_bytes(body)
        with closing(self.connect()) as db,db:
            changed=db.execute("UPDATE calls SET status=?,settled_at=?,elapsed_ms=?,http_status=?,metadata_json=? WHERE sequence=? AND status='intent'",
                (status,now(),round((time.perf_counter()-started)*1000),status_code,canonical(_safe_metadata(metadata)),sequence)).rowcount
            if changed!=1:raise EvaluationStopped('本HTTP已经结算，不能覆盖')
            if status!='succeeded':db.execute("UPDATE evaluation_stage_approvals SET status='stopped' WHERE stage_id=?",(self.stage,))

    def stop(self,reason='stopped'):
        self.stopped=True
        with closing(self.connect()) as db,db:
            db.execute('UPDATE evaluation_stage_approvals SET status=? WHERE stage_id=?',(reason,self.stage))

    def report(self):
        with closing(self.connect()) as db:
            rows=db.execute('SELECT sequence,role,context,status,http_status FROM calls WHERE sequence>? ORDER BY sequence',(self.start,)).fetchall()
        return {'stage_id':self.stage,'start_sequence':self.start,'max_model_http':80,'used':len(rows),'remaining':80-len(rows),
            'completed_stream_evidence':sorted(self.plan.get('completed_stream_proofs',{})),
            'calls':[dict(zip(('sequence','role','context','status','http_status'),r)) for r in rows]}


class RecordedStream(httpx.SyncByteStream):
    def __init__(self,stream,budget,sequence,status,started):
        self.stream,self.budget,self.sequence,self.status,self.started=stream,budget,sequence,status,started
        self.parts=[];self.done=False;self.settled=False
    def __iter__(self):
        try:
            for chunk in self.stream:self.parts.append(chunk);yield chunk
            self.done=True
        finally:
            if not self.settled:
                body=b''.join(self.parts)
                self.budget.settle(self.sequence,body,self.status,self.started,uncertain=not self.done and not (self.status==200 and completed_sse(body)));self.settled=True
    def close(self):
        try:self.stream.close()
        finally:
            if not self.settled:
                body=b''.join(self.parts)
                self.budget.settle(self.sequence,body,self.status,self.started,uncertain=not self.done and not (self.status==200 and completed_sse(body)));self.settled=True


class MeteredTransport(httpx.BaseTransport):
    def __init__(self,budget,inner):self.budget,self.inner=budget,inner
    def handle_request(self,request):
        sequence=self.budget.reserve(request);started=time.perf_counter()
        try:response=self.inner.handle_request(request)
        except BaseException:
            self.budget.settle(sequence,b'',None,started,uncertain=True);raise
        if response.is_stream_consumed:
            self.budget.settle(sequence,response.content,response.status_code,started)
        else:response.stream=RecordedStream(response.stream,self.budget,sequence,response.status_code,started)
        return response
    def close(self):self.inner.close()
