"""知识库任务：短事务领取、每个HTTP请求预扣、检查点恢复与真实执行节点。"""
from contextlib import contextmanager
from hashlib import sha256
import json
import time
from uuid import uuid4

from fastapi import HTTPException

from app.repository import now_iso, lease_expiry_iso
from app.question_jobs import _usage_metadata
from app.resource_governance import task_owner

KNOWLEDGE_PROMPT_VERSION = 'knowledge-task-v2'
KNOWLEDGE_RULE = '\n这是知识库多文档问答。不同资料的观点冲突时分别引用，不擅自调和。先用自然语言直答，再按问题需要解释；避免逐句重复“文档指出”。'


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return sha256(dump(value).encode()).hexdigest()


def configuration(settings, embedding):
    from app.answer_semantic_review import policy as review_policy
    from app.rag_prompts import TASK_PROMPT_VERSION
    from app.conversation_memory import MEMORY_SIGNATURE
    from app.rag_pipeline import PIPELINE_VERSION
    from app.rag_phrase_match import PHRASE_POLICY_VERSION
    from app.query_planning import QUERY_PLAN_VERSION
    from app.retrieval_planning import RETRIEVAL_STRATEGY_VERSION
    from app.rag_evidence_slots import SLOT_PLAN_VERSION
    from app.rag_source_diagnostics import SOURCE_DIAGNOSTIC_VERSION
    from app.rag_extract_context import SELECTION_VERSION
    from app.rag_numeric_roles import NUMERIC_ROLE_VERSION
    from app.rag_math_evidence import MATH_EVIDENCE_VERSION
    from app.rag_slot_ranges import SLOT_RANGE_VERSION
    from app.pdf_page_labels import PAGE_LABEL_VERSION
    from app.rag_presentation import PRESENTATION_VERSION
    from app.rag_answer_contract import ANSWER_CONTRACT_VERSION
    from app.rag_answer_strategy import policy_signature
    from app.rag_composition import COMPOSITION_VERSION
    from app.query_understanding import policy
    from app.intent_execution import EXECUTION_VERSION
    return fingerprint([EXECUTION_VERSION,policy(settings),review_policy(settings),PIPELINE_VERSION,QUERY_PLAN_VERSION,RETRIEVAL_STRATEGY_VERSION,SLOT_PLAN_VERSION,SOURCE_DIAGNOSTIC_VERSION,
        SELECTION_VERSION,NUMERIC_ROLE_VERSION,MATH_EVIDENCE_VERSION,SLOT_RANGE_VERSION,PAGE_LABEL_VERSION,PRESENTATION_VERSION,ANSWER_CONTRACT_VERSION,policy_signature(),COMPOSITION_VERSION,PHRASE_POLICY_VERSION,embedding.signature, settings.deepseek_base_url, settings.deepseek_model,
        settings.deepseek_thinking, TASK_PROMPT_VERSION, KNOWLEDGE_PROMPT_VERSION, KNOWLEDGE_RULE, settings.question_context_max_chars,
        settings.question_input_max_chars, settings.question_context_k,settings.question_candidate_k,
        settings.rag_retrieval_k,settings.rag_min_score,MEMORY_SIGNATURE])


class TaskStopped(Exception):
    def __init__(self, code, message, status='failed'):
        self.code, self.message, self.status = code, message, status


class KnowledgeTasks:
    def __init__(self, repository):
        self.repository = repository

    def row(self, identifier):
        with self.repository.connect() as db:
            row = db.execute('SELECT * FROM kb_tasks WHERE id=?', (identifier,)).fetchone()
        return dict(row) if row else None

    def create(self, kind, payload, key, *, base_id=None, document_id=None, owner=None, max_requests=8, conversation_id=None):
        identifier, stamp, encoded = 'ktask-'+uuid4().hex, now_iso(), dump(payload)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM kb_tasks WHERE owner_user_id IS ? AND kind=? AND request_key=?', (owner,kind,key)).fetchone()
            if prior:
                if prior['input_hash']!=fingerprint(payload): raise HTTPException(409,'请求键已用于不同的输入')
                return dict(prior)
            if payload.get('operation')=='intent_examples':
                active=db.execute("SELECT * FROM kb_tasks WHERE kind='index' AND status IN ('queued','running') AND json_extract(input_json,'$.operation')='intent_examples' AND json_extract(input_json,'$.semantic_configuration')=?",(payload['semantic_configuration'],)).fetchone()
                if active:return dict(active)
            if conversation_id:
                conv=db.execute('SELECT * FROM kb_conversations WHERE id=? AND owner_user_id IS ?', (conversation_id,owner)).fetchone()
                if not conv or conv['deleted_at'] or conv['archived'] or conv['base_id']!=base_id or conv['memory_epoch']!=payload.get('memory_epoch'):
                    raise HTTPException(409,'会话范围或记忆已变更，请刷新后提交')
                if db.execute("SELECT 1 FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status IN ('queued','running')", (conversation_id,)).fetchone():
                    raise HTTPException(409,'本会话已有活动任务')
                if payload.get('submission_id'):
                    submission=db.execute('SELECT * FROM chat_submissions WHERE id=?',(payload['submission_id'],)).fetchone()
                    if not submission or submission['status']!='waiting' or submission['conversation_id']!=conversation_id or submission['owner_user_id']!=owner:raise HTTPException(409,'等待请求已取消或会话已变化')
            if kind=='index' and db.execute("SELECT 1 FROM kb_tasks WHERE document_id=? AND kind='index' AND status IN ('queued','running')", (document_id,)).fetchone():
                raise HTTPException(409,'文档已有索引任务，请刷新查看进度')
            from app.resource_governance import admit
            admit(db,self.repository,owner,submission_id=payload.get('submission_id'),ingestion_id=payload.get('ingestion_id'))
            db.execute('INSERT INTO kb_tasks(id,kind,base_id,document_id,owner_user_id,request_key,input_json,input_hash,max_requests,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,kind,base_id,document_id,owner,key,encoded,fingerprint(payload),max_requests,stamp,stamp))
            if conversation_id:
                number=db.execute('SELECT COALESCE(MAX(turn_no),0)+1 FROM kb_conversation_turns WHERE conversation_id=?',(conversation_id,)).fetchone()[0]
                if number>100:raise HTTPException(409,'会话已达100轮，请新建会话')
                db.execute('INSERT INTO kb_conversation_turns VALUES (?,?,?)',(conversation_id,number,identifier))
                clarification=payload.get('understanding',{}).get('clarification')
                if clarification and payload.get('route',{}).get('code')=='clarification-needed':
                    db.execute('INSERT INTO conversation_clarifications(id,task_id,conversation_id,owner_user_id,epoch,needed_count,choices_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                        ('clarify-'+identifier,identifier,conversation_id,owner,payload['memory_epoch'],clarification['needed_count'],dump(clarification['choices']),stamp,stamp))
                db.execute("UPDATE kb_conversations SET updated_at=?,title=CASE WHEN title='新会话' THEN ? ELSE title END WHERE id=?",(stamp,payload.get('question','')[:60],conversation_id))
        return self.row(identifier)

    @staticmethod
    def allowed(db, row):
        from app.knowledge_scope import base_allowed,scope_documents
        if row['base_id'] and not base_allowed(db,row['base_id'],row['owner_user_id'],write=row['kind']=='index'):return False
        payload=json.loads(row['input_json'])
        if row['kind']=='index' and payload.get('ingestion_id'):
            ingestion=db.execute('SELECT * FROM ingestion_jobs WHERE id=?',(payload['ingestion_id'],)).fetchone()
            if not ingestion or ingestion['base_id']!=row['base_id'] or ingestion['document_id']!=row['document_id'] or ingestion['owner_user_id']!=row['owner_user_id']:return False
            if row['status'] in ('queued','running') and ingestion['status'] not in ('queued','running'):return False
        if row['owner_user_id']:
            actor=db.execute('SELECT role,active FROM product_users WHERE id=?',(row['owner_user_id'],)).fetchone()
            if not actor or not actor['active']:return False
            if row['kind']=='index' and actor['role']!='admin':
                payload=json.loads(row['input_json'])
                if not payload.get('ingestion_id'):return False
        if row['kind']=='question':
            payload=json.loads(row['input_json'])
            if payload.get('submission_id') and row['status'] in ('queued','running'):
                submission=db.execute('SELECT status FROM chat_submissions WHERE id=?',(payload['submission_id'],)).fetchone()
                if not submission or submission[0] not in ('waiting','submitted'):return False
            anchor=payload.get('legacy_anchor')
            if anchor and anchor['source']=='document':
                original=db.execute('SELECT archived,deleted_at,owner_user_id FROM conversations WHERE id=?',(anchor['id'],)).fetchone()
                if not original or original['deleted_at'] or original['owner_user_id']!=row['owner_user_id'] or row['status'] in ('queued','running') and original['archived']:return False
            if payload.get('conversation_id'):
                conv=db.execute('SELECT * FROM kb_conversations WHERE id=?',(payload['conversation_id'],)).fetchone()
                if not conv or conv['deleted_at'] or row['status'] in ('queued','running') and conv['archived']:return False
            if payload.get('conversation_id') and row['status'] in ('queued','running'):
                conv=db.execute('SELECT memory_epoch FROM kb_conversations WHERE id=?',(payload['conversation_id'],)).fetchone()
                if not conv or conv[0]!=payload.get('memory_epoch'):return False
            base=db.execute('SELECT active_snapshot_id FROM knowledge_bases WHERE id=?',(row['base_id'],)).fetchone()
            if not base or not base[0]:return payload.get('route',{}).get('kind')=='SYSTEM'
            current=scope_documents(db,row['base_id'])
            if not {d['document_id'] for d in payload.get('documents',[])}<=current:return False
            if payload.get('tool_revision') is not None and row['status'] in ('queued','running'):
                tool=db.execute('SELECT enabled,revision FROM mcp_tools WHERE id=?',(payload['route'].get('tool_id'),)).fetchone()
                if not tool or not tool['enabled'] or tool['revision']!=payload['tool_revision']:return False
            if row['status'] in ('queued','running'):
                resolved=db.execute("SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name='resolved_intent'",(row['id'],)).fetchone()
                route=json.loads(resolved[0]).get('route',{}) if resolved else {}
                if route.get('kind')=='MCP':
                    tool=db.execute('SELECT enabled,revision FROM mcp_tools WHERE id=?',(route.get('tool_id'),)).fetchone()
                    if not tool or not tool['enabled'] or tool['revision']!=payload.get('semantic_tool_revisions',{}).get(route.get('tool_id')):return False
        return True

    @classmethod
    def valid(cls, db, identifier, token):
        row=db.execute('SELECT * FROM kb_tasks WHERE id=?',(identifier,)).fetchone()
        return bool(row and row['status']=='running' and row['lease_token']==token and row['lease_expires_at']>now_iso()
                    and not row['cancel_requested'] and cls.allowed(db,row))

    def claim(self, worker, seconds, identifier=None):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            expired=db.execute("SELECT id FROM kb_tasks WHERE status='running' AND lease_expires_at<=?",(now_iso(),)).fetchall()
            for entry in expired:
                pending=db.execute("SELECT 1 FROM kb_task_calls WHERE task_id=? AND status='intent'",(entry[0],)).fetchone()
                db.execute("UPDATE kb_task_calls SET status='uncertain',error_code='call_uncertain',finished_at=? WHERE task_id=? AND status='intent'",(now_iso(),entry[0]))
                db.execute("UPDATE kb_trace_spans SET status='interrupted',finished_at=?,error_code='lease_expired' WHERE task_id=? AND status='running'",(now_iso(),entry[0]))
                db.execute('UPDATE kb_tasks SET status=?,error_code=?,error_message=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?',
                    ('needs_attention' if pending else 'queued','call_uncertain' if pending else None,'存在未确认调用，请核实后明确重试' if pending else None,entry[0]))
            from app.resource_governance import choose
            row=choose(db,self.repository,'kb_tasks',identifier)
            if not row:return None
            if not self.allowed(db,row):
                db.execute("UPDATE kb_tasks SET status='cancelled',error_code='access_revoked',error_message='账号、会话或资料权限已变更',finished_at=? WHERE id=?",(now_iso(),row['id']))
                return None
            token=worker+':'+uuid4().hex
            db.execute("UPDATE kb_tasks SET status='running',lease_token=?,lease_expires_at=?,attempt_count=attempt_count+1,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                (token,lease_expiry_iso(seconds),now_iso(),now_iso(),row['id']))
        return self.row(row['id'])

    def check(self, identifier, token):
        with self.repository.connect() as db:
            if not self.valid(db,identifier,token):raise TaskStopped('task_stopped','任务已停止、租约失效或权限已变化','cancelled')

    def renew(self, identifier, token, seconds):
        with self.repository.connect() as db:
            if not self.valid(db,identifier,token):return False
            return bool(db.execute('UPDATE kb_tasks SET lease_expires_at=? WHERE id=? AND lease_token=?',(lease_expiry_iso(seconds),identifier,token)).rowcount)

    def stage(self, identifier, token, stage):
        self.check(identifier,token)
        with self.repository.connect() as db:db.execute('UPDATE kb_tasks SET stage=?,updated_at=? WHERE id=? AND lease_token=?',(stage,now_iso(),identifier,token))

    @contextmanager
    def span(self, job, name, kind, parent=None):
        self.check(job['id'],job['lease_token'])
        identifier='span-'+uuid4().hex;start=time.perf_counter()
        with self.repository.connect() as db:
            db.execute("INSERT INTO kb_trace_spans(id,task_id,attempt_no,parent_id,name,kind,status,started_at) VALUES (?,?,?,?,?,?,'running',?)",
                (identifier,job['id'],job['attempt_count'],parent,name,kind,now_iso()))
        status,code='succeeded',None
        try:yield identifier
        except Exception as exc:
            status,code='failed',exc.code if isinstance(exc,TaskStopped) else 'step_failed'
            raise
        finally:
            with self.repository.connect() as db:db.execute('UPDATE kb_trace_spans SET status=?,finished_at=?,elapsed_ms=?,error_code=? WHERE id=?',(status,now_iso(),round((time.perf_counter()-start)*1000),code,identifier))

    def checkpoint(self, identifier, name):
        with self.repository.connect() as db:row=db.execute('SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name=?',(identifier,name)).fetchone()
        return json.loads(row[0]) if row else None

    def save_local_resolution(self,job,resolution,name='resolved_intent'):
        encoded=dump(resolution)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.valid(db,job['id'],job['lease_token']):raise TaskStopped('task_stopped','执行状态已变化','cancelled')
            prior=db.execute('SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name=?',(job['id'],name)).fetchone()
            if prior and prior[0]!=encoded:raise TaskStopped('intent_changed','语义策略或对象规划已变化，请新建任务')
            db.execute('INSERT OR IGNORE INTO kb_task_checkpoints VALUES (?,?,?,?)',(job['id'],name,encoded,now_iso()))

    def diagnostic(self, job, kind, payload):
        """诊断不是成功检查点；不能供恢复路径跳过校验或再次触发模型调用。"""
        if kind not in {'retrieval','validation'}:raise ValueError('invalid_diagnostic_kind')
        name=f"diagnostic_{kind}_attempt_{job['attempt_count']}"
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.valid(db,job['id'],job['lease_token']):return False
            db.execute('INSERT OR IGNORE INTO kb_task_checkpoints VALUES (?,?,?,?)',
                (job['id'],name,dump(payload),now_iso()))
        return True

    @task_owner
    def call(self, job, role, work, *, checkpoint=None, validate=lambda value:value):
        saved=self.checkpoint(job['id'],checkpoint) if checkpoint else None
        if saved is not None:return validate(saved)
        identifier,token=job['id'],job['lease_token']
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.valid(db,identifier,token):raise TaskStopped('task_stopped','任务已停止','cancelled')
            row=db.execute('SELECT * FROM kb_tasks WHERE id=?',(identifier,)).fetchone()
            if row['requests_used']>=row['max_requests']:raise TaskStopped('budget_exhausted','任务请求预算不足，未发起新的请求','needs_attention')
            sequence=db.execute('SELECT COALESCE(MAX(sequence_no),0)+1 FROM kb_task_calls WHERE task_id=?',(identifier,)).fetchone()[0]
            db.execute('UPDATE kb_tasks SET requests_used=requests_used+1 WHERE id=?',(identifier,))
            db.execute("INSERT INTO kb_task_calls(id,task_id,sequence_no,role,status,started_at,delivery_status) VALUES (?,?,?,?,'intent',?,'reserved')",('kcall-'+uuid4().hex,identifier,sequence,role,now_iso()))
        started=time.perf_counter();raw=usage=None
        try:
            raw,usage=work()
            value=validate(raw)
            with self.repository.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # 调用完成即结算，即使中途撤权也保留费用记录，但不能发布正文。
                db.execute("UPDATE kb_task_calls SET status='succeeded',delivery_status='sent',usage_json=?,finished_at=?,elapsed_ms=? WHERE task_id=? AND sequence_no=?",(dump(_usage_metadata(usage)),now_iso(),round((time.perf_counter()-started)*1000),identifier,sequence))
                if self.valid(db,identifier,token) and checkpoint:
                    db.execute('INSERT OR IGNORE INTO kb_task_checkpoints VALUES (?,?,?,?)',(identifier,checkpoint,dump(value),now_iso()))
            self.check(identifier,token)
            return value
        except Exception as exc:
            from app.rag_validation import ModelOutputError
            if getattr(exc, 'not_sent', False):
                from app.call_accounting import settle_not_sent
                with self.repository.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    settle_not_sent(db, 'kb_task_calls', identifier, sequence)
                raise TaskStopped('not_sent', '配额、熔断或执行状态阻止发送，未消耗调用额度；可稍后明确重试') from None
            uncertain=(getattr(exc,'uncertain',False) or getattr(exc,'status_code',None)==504 or isinstance(exc,TaskStopped) and exc.status=='needs_attention'
                or not hasattr(exc,'status_code') and not isinstance(exc,(TaskStopped,ValueError,ModelOutputError)))
            code='output_'+exc.reason if isinstance(exc,ModelOutputError) else 'call_uncertain' if uncertain else 'call_failed'
            if role=='generation' and isinstance(exc,ModelOutputError):
                self.diagnostic(job,'validation',dict(reason=exc.reason,location=exc.location,
                    sequence_no=sequence,output=raw if isinstance(raw,(str,dict)) else None))
            with self.repository.connect() as db:
                db.execute("UPDATE kb_task_calls SET status=?,delivery_status=?,error_code=?,usage_json=?,finished_at=?,elapsed_ms=? WHERE task_id=? AND sequence_no=? AND status='intent'",
                    ('uncertain' if uncertain else 'failed','uncertain' if uncertain else 'sent',code,dump(_usage_metadata(usage)),now_iso(),round((time.perf_counter()-started)*1000),identifier,sequence))
            if isinstance(exc,TaskStopped):raise
            messages = {
                'output_composition_plan_invalid': '证据选材与段落计划无效，未发起正文生成',
                'output_composition_reference_invalid': '段落计划或正文使用了范围外的证据单元，未发布结果',
                'output_composition_coverage_invalid': '段落计划的子问覆盖无效，未发起正文生成',
                'output_composition_layout_mismatch': '正文未按已确认的段落计划组织，未发布结果',
                'output_composition_comparison_unpaired': '比较段缺少同时引用双方依据的论断，未发布结果',
                'output_composition_checkpoint_changed': '计划与当前证据身份不一致，未继续调用',
                'output_presentation_missing': '回答缺少正文组织信息，未发布完整结果；请查看链路详情',
                'output_presentation_invalid': '回答的正文组织格式无效，未发布完整结果；请查看链路详情',
                'output_presentation_reference_invalid': '正文组织引用了不存在的事实，未发布完整结果',
                'output_presentation_coverage_mismatch': '正文组织存在事实遗漏或重复，未发布完整结果',
                'output_numeric_context_missing': '回答中的数值缺少适用限定，未发布完整结果；请查看链路详情',
                'output_technical_literal_not_supported': '回答中的协议标识或状态码缺少本条原文支持，未发布完整结果；请核对技术标识',
                'output_mixed_user_source': '回答将题干中的表述作为原文引用，但本条证据未支持；请查看链路中的失败论断位置',
                'output_number_not_in_quote': '回答中的数值未在本条引用原文中找到，未发布完整结果；请核对数字及其适用对象',
                'output_number_unit_mismatch': '回答中的数值单位与本条引用原文不一致，未发布完整结果',
                'output_page_locator_not_supported': '回答标注的页码与本条引用来源不一致，未发布完整结果',
                'output_statement_locator_not_supported': '回答标注的命题、推论或条目编号缺少本条原文支持，未发布完整结果',
                'output_locator_not_supported': '回答中的文内定位编号缺少本条来源支持，未发布完整结果；请核对章节、图表或条目位置',
            }
            message = ('调用结果不确定，请核实后明确重试' if uncertain else
                       messages.get(code, '调用或输出校验失败；未发布结果'))
            raise TaskStopped(code,message,'needs_attention' if uncertain else 'failed') from None

    def finish(self, job, status, *, result=None, code=None, message=None):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM kb_tasks WHERE id=?',(job['id'],)).fetchone()
            if not row or row['lease_token']!=job['lease_token'] or row['status']!='running':return False
            if status=='succeeded' and not self.valid(db,job['id'],job['lease_token']):status,result,code,message='cancelled',None,'task_stopped','资料权限或会话状态已变化，结果未发布'
            payload=json.loads(row['input_json'])
            if status=='succeeded' and result and payload.get('semantic_review',{}).get('enabled') and result.get('status') in {'answered','partial'}:
                from app.answer_semantic_review import verify_stored_publication
                records={r['name']:json.loads(r['payload_json']) for r in db.execute(
                    "SELECT name,payload_json FROM kb_task_checkpoints WHERE task_id=? AND name IN ('semantic_review_input','semantic_review')",(job['id'],))}
                verify_stored_publication(result,records.get('semantic_review_input',{}),records.get('semantic_review'),payload['question'])
            if status=='succeeded' and result and result.get('clarification',{}).get('kind')=='tool_parameters':
                payload=json.loads(row['input_json']);clar=result['clarification'];stamp=now_iso()
                db.execute('INSERT INTO conversation_clarifications(id,task_id,conversation_id,owner_user_id,epoch,needed_count,choices_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                    (clar['id'],row['id'],payload['conversation_id'],row['owner_user_id'],payload['memory_epoch'],clar['needed_count'],'[]',stamp,stamp))
            db.execute('UPDATE kb_tasks SET status=?,stage=?,result_json=?,error_code=?,error_message=?,finished_at=?,updated_at=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?',
                (status,'done' if status=='succeeded' else row['stage'],dump(result) if result is not None else None,code,message,now_iso(),now_iso(),job['id']))
        return status=='succeeded'
