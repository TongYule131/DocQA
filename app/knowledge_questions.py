"""库级会话固定发布快照；客户范围在召回前确认。"""
import json
from uuid import uuid4
from fastapi import HTTPException

from app.knowledge_tasks import KnowledgeTasks, configuration, dump
from app.repository import now_iso


class KnowledgeQuestions:
    def __init__(self, access, rag):
        self.access,self.rag,self.repository=access,rag,access.repository
        self.tasks=KnowledgeTasks(self.repository)

    @property
    def owner(self):return self.access.actor['id'] if self.access.actor else None

    def base_scope(self, identifier, document_ids=None):
        base=self.access.base(identifier)
        if not base['active_snapshot_id']:raise HTTPException(409,'知识库尚未发布')
        with self.repository.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT s.*,d.filename FROM knowledge_base_snapshot_documents s JOIN documents d ON d.id=s.document_id WHERE s.snapshot_id=? ORDER BY s.document_id',(base['active_snapshot_id'],))]
            if document_ids:
                from app.knowledge_scope import current_document
                wanted={current_document(db,identifier,i) for i in document_ids}
                if not wanted<={r['document_id'] for r in rows}:raise HTTPException(409,'所选文档尚未生效或已移除，未改用其他资料')
                rows=[r for r in rows if r['document_id'] in wanted]
        if not rows:raise HTTPException(409,'发布版没有资料')
        if len(rows)>50:raise HTTPException(422,'首版单知识库问答最多50份资料，请分库管理')
        return base,rows

    def create(self, base_id, key, *, allow_preparing=False, document_ids=None):
        if allow_preparing:self.access.base(base_id)
        else:self.base_scope(base_id,document_ids)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM kb_conversations WHERE owner_user_id IS ? AND request_key=?',(self.owner,key)).fetchone()
            if prior:
                if prior['base_id']!=base_id or json.loads(prior['document_filter_json'])!=(document_ids or []):raise HTTPException(409,'请求键已用于其他知识库或文档范围')
                return dict(prior)
            identifier,stamp='kconv-'+uuid4().hex,now_iso()
            db.execute('INSERT INTO kb_conversations(id,base_id,owner_user_id,title,request_key,created_at,updated_at,document_filter_json) VALUES (?,?,?,?,?,?,?,?)',(identifier,base_id,self.owner,'新会话',key,stamp,stamp,dump(document_ids or [])))
        return self.require(identifier)

    def require(self, identifier):
        with self.repository.connect() as db:row=db.execute('SELECT * FROM kb_conversations WHERE id=? AND owner_user_id IS ?',(identifier,self.owner)).fetchone()
        if not row or row['deleted_at']:raise HTTPException(404,'会话不存在或未获授权')
        self.access.base(row['base_id'])
        return dict(row)

    def list(self):
        with self.repository.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM kb_conversations WHERE owner_user_id IS ? AND deleted_at IS NULL ORDER BY updated_at DESC',(self.owner,))]
        result=[]
        for r in rows:
            try:self.access.base(r['base_id']);result.append(r)
            except HTTPException:pass
        return result

    def require_task(self, identifier):
        row=self.tasks.row(identifier)
        if not row:raise HTTPException(404,'任务不存在或未获授权')
        if row['kind']=='index':
            if json.loads(row['input_json']).get('ingestion_id'):
                from app.ingestion import Ingestion
                Ingestion(self.access).require(json.loads(row['input_json'])['ingestion_id'],write=True)
            else:self.access.admin()
        elif row['owner_user_id']!=self.owner:raise HTTPException(404,'任务不存在或未获授权')
        with self.repository.connect() as db:
            if not KnowledgeTasks.allowed(db,row):raise HTTPException(404,'任务不存在或未获授权')
        return row

    def present(self, row):
        value={k:v for k,v in row.items() if k not in {'input_json','input_hash','lease_token','lease_expires_at','result_json'}}
        value['question']=json.loads(row['input_json']).get('question','')
        payload=json.loads(row['input_json'])
        kind=payload.get('route',{}).get('kind')
        value['provider_requests_expected']=payload.get('provider_requests_expected',2) if kind=='KB' else 0 if kind=='SYSTEM' else None
        value['streaming']=bool(json.loads(row['input_json']).get('stream_protocol'))
        value['result']=json.loads(row['result_json']) if row['result_json'] else None
        with self.repository.connect() as db:
            value['calls']=[{k:r[k] for k in ['sequence_no','role','status','error_code','elapsed_ms']} for r in db.execute('SELECT * FROM kb_task_calls WHERE task_id=? ORDER BY sequence_no',(row['id'],))]
        return value

    def detail(self, identifier):
        conv=self.require(identifier)
        with self.repository.connect() as db:ids=[r[0] for r in db.execute('SELECT task_id FROM kb_conversation_turns WHERE conversation_id=? ORDER BY turn_no',(identifier,))]
        conv['turns']=[self.present(self.require_task(i)) for i in ids]
        conv['base']=self.access.base(conv['base_id'])
        return conv

    def submit(self, identifier, question, key, *, document_ids=None, submission_id=None):
        conv=self.require(identifier)
        if conv['archived']:raise HTTPException(409,'会话已归档，请恢复或新建会话')
        document_ids=json.loads(conv['document_filter_json']) if document_ids is None else document_ids
        # 请求键先复用；刷新或原请求重发不改变快照、不重新收费。
        with self.repository.connect() as db:
            prior=db.execute("SELECT j.* FROM kb_tasks j JOIN kb_conversation_turns t ON t.task_id=j.id WHERE t.conversation_id=? AND j.request_key=?",(identifier,identifier+'/'+key)).fetchone()
            if prior:
                previous=json.loads(prior['input_json'])
                if previous['question']!=question or previous.get('document_filter',[])!=document_ids:raise HTTPException(409,'请求键已用于不同问题或文档范围')
                return self.present(self.require_task(prior['id']))
            history=[json.loads(r[0])['question'] for r in db.execute("SELECT j.input_json FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status='succeeded' AND json_extract(j.input_json,'$.memory_epoch')=? AND json_extract(j.input_json,'$.route.kind')!='SYSTEM' ORDER BY t.turn_no DESC LIMIT 3",(identifier,conv['memory_epoch']))][::-1] if conv['memory_enabled'] else []
            anchor=db.execute('SELECT source,source_id FROM unified_chat_links WHERE conversation_id=?',(identifier,)).fetchone()
        if conv['memory_enabled'] and conv['memory_epoch']==0:history=(json.loads(conv['initial_context_json'])+history)[-3:]
        history=[q[:600] for q in history]
        from app.conversation_memory import MEMORY_VERSION,retrieval_question
        memory={'version':MEMORY_VERSION,'notes':conv['memory_notes'] if conv['memory_enabled'] else '','recent_questions':history}
        base=self.access.base(conv['base_id'])
        from app.intent_management import IntentManagement
        resolution=IntentManagement(self.repository).evaluate(question,base['id']);route=resolution['route']
        docs=[] if route['kind']=='SYSTEM' else self.base_scope(conv['base_id'],document_ids)[1]
        frozen=[]
        for doc in docs:
            snapshot=self.rag.capture_snapshot(doc['document_id'],question,published=doc) if route['kind']=='KB' else None
            frozen.append({'snapshot':snapshot,'filename':doc['filename'],'document_id':doc['document_id']})
        payload={'question':question,'documents':frozen,'snapshot_id':base['active_snapshot_id'],
                 'configuration':configuration(self.access.settings,self.access.index.embedding),
                 'conversation_id':identifier,'memory_epoch':conv['memory_epoch'],'recent_questions':history,
                 'memory_notes':conv['memory_notes'] if conv['memory_enabled'] else '',
                 'document_filter':document_ids,'route':route,'intent_decision':resolution['decision'],
                 'query_plan':resolution['query_plan']}
        from app.query_planning import execution_budget
        payload['retrieval_budget']=execution_budget(resolution['query_plan'],self.access.settings,route)
        payload['retrieval_question']=retrieval_question(question,memory) if memory['notes'] or history else question
        if anchor:payload['legacy_anchor']={'source':anchor[0],'id':anchor[1]}
        if submission_id:payload['submission_id']=submission_id
        if route['kind']=='KB':
            from app.answer_stream import STREAM_VERSION
            from app.rag_composition import needs_plan
            payload['stream_protocol']=STREAM_VERSION
            # 随任务冻结；不能用新版分类器反算旧任务曾经需要的请求数。
            payload['provider_requests_expected']=2+int(needs_plan(question))
        if route['kind']=='MCP':
            from app.knowledge_tools import ToolRegistry
            tool=ToolRegistry(self.repository).get(route.get('tool_id'))
            if not tool or not tool['enabled']:raise HTTPException(409,'此意图的工具尚未启用')
            payload['tool_revision']=tool['revision']
        row=self.tasks.create('question',payload,identifier+'/'+key,base_id=base['id'],owner=self.owner,max_requests=8,conversation_id=identifier)
        return self.present(row)

    def memory(self, identifier, enabled, clear):
        self.require(identifier)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status IN ('queued','running')",(identifier,)).fetchone():raise HTTPException(409,'请等待本轮完成或停止后修改记忆')
            db.execute('UPDATE kb_conversations SET memory_enabled=?,memory_epoch=memory_epoch+1,updated_at=? WHERE id=?',(int(enabled),now_iso(),identifier))
            # 清除通过epoch隔离，旧历史仍可审计，但不再进入记忆。
            if clear:
                db.execute('UPDATE kb_conversations SET memory_enabled=0 WHERE id=?',(identifier,))
        return self.detail(identifier)
