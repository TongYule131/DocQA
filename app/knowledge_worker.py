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
        if route['kind']=='SYSTEM':
            return {'response_kind':'system','answer':route['response_text'],'status':'answered','citations':[],'conclusion':[],
                'explanation':[],'limitations':[],'route':route['code'],'prompt_version':'system-intent-v1'}
        if route['kind']=='MCP':
            from app.knowledge_tools import execute_tool
            return execute_tool(self,job,payload,route,parent)
        self.check(job)
        original_index=DocumentIndex(self.repository,self.embedding)
        search_question=payload.get('retrieval_question',payload['question'])
        for doc in payload['documents']:original_index.verify_snapshot(doc['document_id'],doc['snapshot'])
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
        index=DocumentIndex(self.repository,QueryEmbedding());packs=[]
        system=build_task_system_prompt()+KNOWLEDGE_RULE
        context_k=min(self.settings.question_context_k,route.get('top_k') or self.settings.question_context_k)
        memory=payload.get('recent_questions',[])
        if memory or payload.get('memory_notes'):
            from app.conversation_memory import MEMORY_RULE
            system+=MEMORY_RULE
        budget=min(self.settings.question_context_max_chars,self.settings.question_input_max_chars-len(system)-len(payload['question'])-3500)
        if budget<1000:raise TaskStopped('input_budget_exceeded','问题与系统规则超过输入预算')
        with self.tasks.span(job,'库内多文档召回与证据装入','RETRIEVE',parent):
            for doc in payload['documents']:
                self.check(job)
                found=index.search_snapshot(doc['document_id'],search_question,min(20,self.settings.rag_retrieval_k),snapshot=doc['snapshot'],include_scores=True)
                from app.retrieval_planning import plan_retrieval
                found=plan_retrieval(payload['question'],found,self.repository.chunks(doc['document_id'],doc['snapshot']['parse_version_id']),max_candidates=32,fulltext_budget=budget,max_items=self.settings.question_context_k,payload_transform=task_segment_payload)
                built=build_evidence(found,self.settings,expected_document_id=doc['document_id'],max_items=self.settings.question_context_k,
                    context_max_chars=budget,payload_transform=task_segment_payload,table_blocks=self.repository.blocks(doc['snapshot']['parse_version_id']),
                    extractive_question=payload['question'])
                if built.fatal_reason:raise TaskStopped('source_scope_invalid','证据来源范围无效')
                packs.append(built.pack)
            candidates=[]
            # 首轮按文档最佳命中评分交错，后续按层补入；不让单文档占满所有证据。
            packs.sort(key=lambda p:max((i.score for i in p.items),default=0),reverse=True)
            for n in range(max((len(p.items) for p in packs),default=0)):
                candidates.extend(p.items[n] for p in packs if len(p.items)>n)
            pairs=[p for pack in packs for p in pack.continuation_pairs]
            groups={i.chunk_id:{i.chunk_id} for i in candidates}
            for p in pairs:
                a,b=p['left_chunk_id'],p['right_chunk_id']
                if a in groups and b in groups:
                    union=groups[a]|groups[b]
                    for key in union:groups[key]=union
            def serialize(items):
                assign_unit_options(items,pairs)
                return dump(task_segment_payload([i.payload() for i in items]))
            order={i.chunk_id:n for n,i in enumerate(candidates)}
            items,truncated,chars,drops=pack_evidence(candidates,context_max_chars=budget,serialize=serialize,max_items=context_k,partners={k:sorted(v,key=order.get) for k,v in groups.items()})
            assign_unit_options(items,pairs)
            pack=EvidencePack(items=items,candidates=candidates,index_id=None,parse_version_id=None,truncated=truncated,context_chars=chars,drop_reasons=drops,
                review_policy=packs[0].review_policy if packs else None,continuation_pairs=pairs)
        if not items:return {'answer':INSUFFICIENT_EVIDENCE_TEXT,'status':'insufficient_evidence','citations':[],'conclusion':[],'explanation':[],'limitations':['本次检索未得到可用证据']}
        self.tasks.stage(job['id'],job['lease_token'],'generating')
        names={d['document_id']:d['filename'] for d in payload['documents']}
        entries=[dict(i.payload(),document_name=names[i.document_id]) for i in items]
        user=build_task_user_prompt(question=payload['question'],document_name='知识库资料',evidence_payload=entries)
        if memory or payload.get('memory_notes'):
            from app.conversation_memory import MEMORY_VERSION
            value=json.loads(user);value['conversation_context']={'version':MEMORY_VERSION,'notes':payload.get('memory_notes',''),'recent_questions':memory};user=dump(value)
        if len(system)+len(user)>self.settings.question_input_max_chars:raise TaskStopped('input_budget_exceeded','证据与规则超出输入预算，未发起生成')
        rag=RagService(self.settings,self.repository,original_index,self.model)
        def validate(value):
            checked=validate_task_output(dump(value),pack,question=payload['question'],contract_version=ANSWER_CONTRACT_VERSION)
            validate_user_condition_boundary(checked,pack,question=payload['question']);review_claims(checked,pack)
            return value
        def generate():
            from app.answer_stream import STREAM_VERSION,FactStreamParser,checked_block
            if payload.get('stream_protocol')==STREAM_VERSION and hasattr(self.model,'generate_stream'):
                from app.answer_stream_store import AnswerStreamStore
                store=AnswerStreamStore(self.repository)
                def completed(category,ordinal,fact):
                    self.check(job)
                    store.append('knowledge',job,'block',checked_block(category,ordinal,fact,pack,payload['question'],rag,names))
                store.append('knowledge',job,'reset',{'version':STREAM_VERSION})
                checked_at=[0.0]
                def active():
                    if time.perf_counter()-checked_at[0]>=.25:self.check(job);checked_at[0]=time.perf_counter()
                raw=self.model.generate_stream(system,user,FactStreamParser(completed).feed,active)
            else:raw=self.model.generate(system,user)
            return json.loads(raw),getattr(self.model,'last_call_metadata',None)
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
            def paragraph(f):
                text=f.text
                if f.claim_type=='inference':text='据这些内容可推断：'+text
                # 条件字段仍需展示，不能为自然表达而遗漏模型显式给出的限制。
                from app.rag_answer_contract import CONTEXT_LABELS
                details=[CONTEXT_LABELS[c['kind'] if isinstance(c,dict) else c.kind]+'：'+(c['text'] if isinstance(c,dict) else c.text) for c in f.context if (c['text'] if isinstance(c,dict) else c.text) not in text]
                return text+('（'+'；'.join(details)+'）' if details else '')+''.join(f'[{r}]' for r in sorted(f.refs))
            answer='\n\n'.join(paragraph(f) for f in checked.conclusion+checked.explanation)
            if checked.status=='insufficient_evidence':answer=INSUFFICIENT_EVIDENCE_TEXT
            if checked.status=='clarification_needed':answer='需要补充一点信息：\n\n'+'\n'.join(checked.clarification_questions)+'\n\n'+answer
            return dict(answer_id='kanswer-'+job['id'],answer=answer,status=checked.status,conclusion=facts,explanation=explanation,
                citations=[dict(c.model_dump(),filename=names[c.document_id]) for c in citations],clarification_questions=checked.clarification_questions,
                missing_points=checked.missing_points,answer_parts=checked.answer_parts,prompt_version=KNOWLEDGE_PROMPT_VERSION,
                limitations=['只依据本轮入选资料作答，来源匹配不等于全部语义已自动认证']+(['上下文预算限制了入选证据范围'] if truncated else []),
                retrieval={'candidate_count':len(candidates),'selected_count':len(items),'context_chars':chars,'truncated':truncated},route=route['code'],snapshot_id=payload['snapshot_id'])
