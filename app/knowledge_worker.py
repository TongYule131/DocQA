"""复用analysis-worker角色执行库级问答和索引；每批外调均有独立账本。"""
import json
import time
from threading import Event, Thread
from uuid import uuid4

from app.knowledge_tasks import KnowledgeTasks, TaskStopped, configuration, fingerprint, dump, KNOWLEDGE_RULE, KNOWLEDGE_PROMPT_VERSION
from app.vector_index import DocumentIndex, evidence_signature
from app.embedding import APIEmbedding
from app.deepseek import DeepSeekModel
from app.rag import RagService
from app.rag_context import EvidencePack, pack_evidence, build_evidence
from app.rag_prompts import build_task_system_prompt,build_task_user_prompt,INSUFFICIENT_EVIDENCE_TEXT,TASK_PROMPT_VERSION
from app.rag_answer_contract import ANSWER_CONTRACT_VERSION,validate_task_output,fact_display_text
from app.rag_validation import validate_user_condition_boundary
from app.rag_support_review import review_claims
from app.rag_extract_context import assign_unit_options
from app.quote_spans import task_segment_payload


class KnowledgeWorker:
    def __init__(self, settings, repository, runtime, *, embedding=None, model=None):
        self.settings,self.repository,self.runtime=settings,repository,runtime
        repository.settings=settings
        self.embedding=embedding or APIEmbedding(settings);self.model=model or DeepSeekModel(settings)
        self.tasks=KnowledgeTasks(repository);self.worker_id='knowledge-'+uuid4().hex
        self.stop_requested=False

    def request_stop(self):self.stop_requested=True

    def run_once(self, identifier=None):
        self.runtime.check()
        job=self.tasks.claim(self.worker_id,self.settings.worker_lease_seconds,identifier)
        if not job:return False
        done=Event()
        def heartbeat():
            while not done.wait(max(.2,self.settings.worker_lease_seconds/3)):
                if not self.tasks.renew(job['id'],job['lease_token'],self.settings.worker_lease_seconds):return
        thread=Thread(target=heartbeat,daemon=True);thread.start()
        try:
            payload=json.loads(job['input_json'])
            if fingerprint(payload)!=job['input_hash'] or payload['configuration']!=configuration(self.settings,self.embedding):
                raise TaskStopped('configuration_changed','输入或模型配置已变化，请重新提交')
            with self.tasks.span(job,'知识库处理','PIPELINE') as parent:
                result=self.index(job,payload,parent) if job['kind']=='index' else self.answer(job,payload,parent)
            self.tasks.finish(job,'succeeded',result=result)
        except TaskStopped as exc:self.tasks.finish(job,exc.status,code=exc.code,message=exc.message)
        except Exception:self.tasks.finish(job,'failed',code='processing_failed',message='处理或引用校验未通过，未发布结果；请查看链路节点')
        finally:done.set();thread.join(timeout=2)
        return True

    def check(self, job):
        if self.stop_requested:raise TaskStopped('worker_stopping','服务正在停止；已确认检查点保留','needs_attention')
        self.runtime.check();self.tasks.check(job['id'],job['lease_token'])
        payload=json.loads(job['input_json'])
        if payload.get('ingestion_id'):
            from app.ingestion import ingestion_configuration
            with self.repository.connect() as db:
                ingest=db.execute('SELECT * FROM ingestion_jobs WHERE id=?',(payload['ingestion_id'],)).fetchone()
                base=db.execute('SELECT * FROM knowledge_bases WHERE id=?',(job['base_id'],)).fetchone()
            if not ingest or not base or ingest['configuration']!=ingestion_configuration(self.settings,base):raise TaskStopped('configuration_changed','上传处理配置已变化，未继续外部调用')

    def index(self,job,payload,parent):
        chunks=self.repository.chunks(job['document_id'],payload['version_id'])
        if evidence_signature(chunks)!=payload['evidence_signature']:raise TaskStopped('source_changed','分块内容已变化，索引未执行')
        worker=self
        class AccountedEmbedding:
            settings=worker.embedding.settings
            signature=worker.embedding.signature
            def embed(self,texts):
                vectors=[]
                for offset in range(0,len(texts),self.settings.embedding_batch_size):
                    worker.check(job);batch=texts[offset:offset+self.settings.embedding_batch_size]
                    def call():
                        data=worker.embedding.embed(batch)
                        return data,getattr(worker.embedding,'last_call_metadata',None)
                    from app.embedding import normalize_vector
                    def validate(value):
                        if len(value)!=len(batch):raise ValueError('vector_count_mismatch')
                        return [normalize_vector(v) for v in value]
                    with worker.tasks.span(job,'向量批次 '+str(offset//self.settings.embedding_batch_size+1),'EMBEDDING',parent):
                        vectors.extend(worker.tasks.call(job,'embedding',call,checkpoint='vectors:'+fingerprint(batch),validate=validate))
                return vectors
        self.tasks.stage(job['id'],job['lease_token'],'indexing')
        with self.tasks.span(job,'建立并发布索引','INDEX',parent):
            index=DocumentIndex(self.repository,AccountedEmbedding())
            try:
                index.build(job['document_id'],rebuild=payload['rebuild'],version_id=payload['version_id'],
                    publication_check=lambda db:KnowledgeTasks.valid(db,job['id'],job['lease_token']))
            except Exception:
                self.check(job)
                with self.repository.connect() as db:
                    uncertain=db.execute("SELECT 1 FROM kb_task_calls WHERE task_id=? AND status='uncertain' AND COALESCE(error_code,'')!='retry_acknowledged'",(job['id'],)).fetchone()
                if uncertain:raise TaskStopped('call_uncertain','向量调用结果不确定，核实后再明确重试','needs_attention') from None
                raise
            return index.index_info(job['document_id'])

    def answer(self,job,payload,parent):
        from app.answer_stream import STREAM_VERSION
        if payload.get('stream_protocol') not in (None,STREAM_VERSION):
            raise TaskStopped('stream_protocol_changed','流式协议已变化，请新建任务')
        from app.intent_management import IntentManagement
        self.tasks.stage(job['id'],job['lease_token'],'routing')
        with self.tasks.span(job,'意图识别','INTENT',parent):
            resolution=IntentManagement(self.repository).evaluate(payload['question'],job['base_id']);route=resolution['route']
            if route!=payload.get('route') or (payload.get('intent_decision') and resolution['decision']!=payload['intent_decision']):raise TaskStopped('intent_changed','意图配置已变化，请在当前会话重新提交')
        from app.query_planning import execution_budget
        with self.tasks.span(job,'查询任务与证据需求规划','QUERY_PLAN',parent):
            plan=resolution['query_plan']
            limits=execution_budget(plan,self.settings,route)
            if (payload.get('query_plan',plan)!=plan or payload.get('retrieval_budget',limits)!=limits):
                raise TaskStopped('query_plan_changed','查询计划或证据预算已变化，请重新提交')
        if route['kind']=='SYSTEM':
            return {'response_kind':'system','answer':route['response_text'],'status':'answered','citations':[],'conclusion':[],
                'explanation':[],'limitations':[],'route':route['code'],'prompt_version':'system-intent-v1'}
        if route['kind']=='MCP':
            from app.knowledge_tools import execute_tool
            return execute_tool(self,job,payload,route,parent)
        self.check(job)
        system=build_task_system_prompt(question=payload['question'])+KNOWLEDGE_RULE
        memory=payload.get('recent_questions',[])
        if memory or payload.get('memory_notes') or payload.get('conversation_context',{}).get('entity_context',{}).get('entities'):
            from app.conversation_memory import MEMORY_RULE
            system+=MEMORY_RULE
        def user_prompt(entries,math_state=None):
            value=json.loads(build_task_user_prompt(question=payload['question'],document_name='知识库资料',evidence_payload=entries))
            if memory or payload.get('memory_notes') or payload.get('conversation_context',{}).get('entity_context',{}).get('entities'):
                from app.conversation_memory import MEMORY_VERSION
                value['conversation_context']=payload.get('conversation_context') or {'version':MEMORY_VERSION,'notes':payload.get('memory_notes',''),'recent_questions':memory}
            if 'formula_check' in plan['facets']:
                # 预留最长状态文本的实际序列化长度，生成时只使用最终装入后的真实状态。
                value['mathematical_evidence']={'representation_status':math_state or max(
                    ('readable_text_unverified','unavailable_or_incomplete'),key=len),'semantic_verification':'not_automatically_verified'}
            return dump(value)
        # 用真实序列化开销反推预算，不能先花一次向量请求再发现系统规则装不下。
        overhead=len(system)+len(user_prompt([]))-2  # references=[]的两字符由证据数组替换
        budget=min(limits['context_chars'],self.settings.question_input_max_chars-overhead)
        if budget<1000:raise TaskStopped('input_budget_exceeded','问题、记忆与系统规则超过输入预算，未发起外部请求')
        original_index=DocumentIndex(self.repository,self.embedding)
        search_question=payload.get('retrieval_question',payload['question'])
        # 可选规划也是收费调用，必须先确认所有冻结来源仍有效。
        for doc in payload['documents']:original_index.verify_snapshot(doc['document_id'],doc['snapshot'])
        if self.settings.query_planner_enabled:
            from app.query_understanding import planner_messages,validate_plan
            context=payload.get('conversation_context',{}).get('entity_context',{})
            ps,pu=planner_messages(payload['question'],context)
            if len(ps)+len(pu)>self.settings.question_input_max_chars:
                raise TaskStopped('input_budget_exceeded','问题规划输入超出预算，未外调')
            def planning_call():
                return self.model.generate(ps,pu),getattr(self.model,'last_call_metadata',None)
            try:
                extra=self.tasks.call(job,'query_planning',planning_call,checkpoint='query_understanding',
                    validate=lambda raw:validate_plan(raw,payload['question'],context))
            except TaskStopped as exc:
                # 不确定请求仍需人工核实；只有已确认失败的规划可回到原问题，不重发规划。
                if exc.status=='needs_attention' or exc.code not in ('call_failed',):raise
                extra={'search_terms':[],'subquestions':[]}
                with self.repository.connect() as db:
                    db.execute('INSERT OR IGNORE INTO kb_task_checkpoints VALUES (?,?,?,?)',
                        (job['id'],'query_understanding',dump(extra),__import__('app.repository',fromlist=['now_iso']).now_iso()))
            search_question+='\n检索子问：'+'；'.join(extra['search_terms']+extra['subquestions'])
        self.tasks.stage(job['id'],job['lease_token'],'retrieving')
        with self.tasks.span(job,'查询向量','EMBEDDING',parent):
            def query():
                result=self.embedding.embed([search_question])
                return result,getattr(self.embedding,'last_call_metadata',None)
            from app.embedding import normalize_vector
            def validate_query(value):
                if not isinstance(value,list) or len(value)!=1:raise ValueError('query_shape')
                return [normalize_vector(value[0])]
            vector=self.tasks.call(job,'query_embedding',query,checkpoint='query_vector',validate=validate_query)[0]
        worker=self
        class QueryEmbedding:
            signature=worker.embedding.signature
            settings=worker.embedding.settings
            def embed(self,_texts):return [vector]
        index=DocumentIndex(self.repository,QueryEmbedding())
        with self.tasks.span(job,'库内多文档召回与证据装入','RETRIEVE',parent):
            from app.rag_pipeline import knowledge_context, RetrievalBudget, PipelineError
            try:
                pack, diagnostics=knowledge_context(index,self.repository,payload['documents'],payload['question'],
                    search_question,self.settings,RetrievalBudget(**{**limits,'context_chars':budget}),
                    query_plan=plan,check=lambda:self.check(job),observe=lambda name,kind:self.tasks.span(job,name,kind,parent))
            except PipelineError as exc:
                if str(exc)=='source_quality_invalid':raise TaskStopped('source_quality_invalid','解析质量无效，请先修复来源') from None
                raise TaskStopped('source_scope_invalid','证据来源范围无效') from None
            self.tasks.diagnostic(job,'retrieval',diagnostics)
            items=pack.items
        if not items:return {'answer':INSUFFICIENT_EVIDENCE_TEXT,'status':'insufficient_evidence','citations':[],'conclusion':[],'explanation':[],
            'limitations':['本次检索未得到预算内的完整可用证据，未进行生成或公式核对'],
            'retrieval':{'slots':pack.slot_hints,'math_evidence':pack.math_hints,'coverage':diagnostics['coverage']}}
        self.tasks.stage(job['id'],job['lease_token'],'generating')
        names={d['document_id']:d['filename'] for d in payload['documents']}
        from app.rag_composition import prepare_composition,writer_prompt,validate_writer_plan
        def obtain_plan(plan_system,plan_user,validate):
            if len(plan_system)+len(plan_user)>self.settings.question_input_max_chars:
                raise TaskStopped('input_budget_exceeded','规划输入超出预算，未发起调用')
            self.tasks.stage(job['id'],job['lease_token'],'planning')
            def generate_plan():return self.model.generate(plan_system,plan_user),getattr(self.model,'last_call_metadata',None)
            with self.tasks.span(job,'证据选材与段落规划','COMPOSITION',parent):
                return self.tasks.call(job,'generation',generate_plan,checkpoint='composition_plan',validate=validate)
        pack,composition_plan,_=prepare_composition(pack,payload['question'],obtain_plan)
        items=pack.items
        self.tasks.stage(job['id'],job['lease_token'],'generating')
        entries=[dict(i.payload(),document_name=names[i.document_id]) for i in items]
        math_slots=[s for s in pack.slot_hints.get('slots',[]) if s['kind'] in {'formula_source','formula_explanation'}]
        math_state=('readable_text_unverified' if len(math_slots)==2 and all(s['state']=='admitted' for s in math_slots)
            and not pack.math_hints.get('incomplete_count') else 'unavailable_or_incomplete')
        user=writer_prompt(user_prompt(entries,math_state),pack,composition_plan)
        if len(system)+len(user)>self.settings.question_input_max_chars:raise TaskStopped('input_budget_exceeded','证据与规则超出输入预算，未发起生成')
        rag=RagService(self.settings,self.repository,original_index,self.model)
        stream_errors=[]
        def validate(value):
            if stream_errors:raise stream_errors[0]
            from app.rag_validation import parse_model_output
            raw=value if isinstance(value,str) else dump(value)
            if composition_plan is not None:validate_writer_plan(raw,composition_plan,pack,question=payload['question'])
            checked=validate_task_output(raw,pack,question=payload['question'],contract_version=ANSWER_CONTRACT_VERSION)
            validate_user_condition_boundary(checked,pack,question=payload['question']);review_claims(checked,pack)
            return parse_model_output(raw)
        def generate():
            from app.answer_stream import STREAM_VERSION,FactStreamParser,checked_block
            if payload.get('stream_protocol')==STREAM_VERSION and hasattr(self.model,'generate_stream'):
                from app.answer_stream_store import AnswerStreamStore
                store=AnswerStreamStore(self.repository)
                def completed(category,ordinal,fact):
                    self.check(job)
                    from app.rag_validation import ModelOutputError
                    if stream_errors:return
                    try:
                        block=checked_block(category,ordinal,fact,pack,payload['question'],rag,names)
                    except ModelOutputError as exc:
                        exc.location.update(category=category,ordinal=ordinal)
                        stream_errors.append(exc)
                    else:
                        block['presentation_block']=parser.presentation_block(category,ordinal)
                        store.append('knowledge',job,'block',block)
                store.append('knowledge',job,'reset',{'version':STREAM_VERSION})
                checked_at=[0.0]
                def active():
                    if time.perf_counter()-checked_at[0]>=.25:self.check(job);checked_at[0]=time.perf_counter()
                parser=FactStreamParser(completed)
                def feed(text):
                    if stream_errors:return
                    from app.rag_validation import ModelOutputError
                    try:parser.feed(text)
                    except ModelOutputError as exc:stream_errors.append(exc)
                # 校验失败后停止展示，仍接收同一HTTP的正文及usage用于诊断；绝不重发。
                raw=self.model.generate_stream(system,user,feed,active)
            else:raw=self.model.generate(system,user)
            return raw,getattr(self.model,'last_call_metadata',None)
        with self.tasks.span(job,'生成与逐论断校验','GENERATION',parent):
            protocol=self.tasks.call(job,'generation',generate,checkpoint='validated_protocol',validate=validate)
        self.tasks.stage(job['id'],job['lease_token'],'validating')
        with self.tasks.span(job,'来源范围重验与保存','VALIDATE',parent):
            self.check(job)
            for doc in payload['documents']:original_index.verify_snapshot(doc['document_id'],doc['snapshot'])
            checked=validate_task_output(dump(protocol),pack,question=payload['question'],contract_version=ANSWER_CONTRACT_VERSION)
            validate_user_condition_boundary(checked,pack,question=payload['question']);review_claims(checked,pack)
            citations=rag._citations(pack,checked.quotes,checked.citation_bindings)
            ids={c.reference_id:c.evidence_id for c in citations}
            def fact(f):return dict(text=f.text,refs=f.refs,context=[c.model_dump() if hasattr(c,'model_dump') else c for c in f.context],claim_type=f.claim_type,support_review=f.support_review,evidence_ids=[ids[r] for r in f.refs])
            facts=[fact(f) for f in checked.conclusion];explanation=[fact(f) for f in checked.explanation]
            from app.rag_presentation import render_answer
            answer=render_answer(checked)
            if checked.status=='insufficient_evidence':answer=INSUFFICIENT_EVIDENCE_TEXT
            return dict(answer_id='kanswer-'+job['id'],answer=answer,status=checked.status,conclusion=facts,explanation=explanation,presentation=checked.presentation,
                citations=[dict(c.model_dump(),filename=names[c.document_id]) for c in citations],clarification_questions=checked.clarification_questions,
                missing_points=checked.missing_points,answer_parts=checked.answer_parts,prompt_version=KNOWLEDGE_PROMPT_VERSION,
                limitations=['只依据本轮入选资料作答，来源匹配不等于全部语义已自动认证']+(['上下文预算限制了入选证据范围'] if pack.truncated else [])+
                    ([f"有{pack.slot_hints['unfilled_count']}个证据需求未装入支持片段，不能据此声称问题已完整覆盖"] if pack.slot_hints.get('unfilled_count') else []),
                retrieval={'candidate_count':len(pack.candidates),'selected_count':len(items),'context_chars':pack.context_chars,
                    'truncated':pack.truncated,'slots':pack.slot_hints,'math_evidence':pack.math_hints,'coverage':diagnostics['coverage']},route=route['code'],snapshot_id=payload['snapshot_id'])
