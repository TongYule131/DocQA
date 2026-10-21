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

    def base_scope(self, identifier):
        base=self.access.base(identifier)
        if not base['active_snapshot_id']:raise HTTPException(409,'知识库尚未发布')
        with self.repository.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT s.*,d.filename FROM knowledge_base_snapshot_documents s JOIN documents d ON d.id=s.document_id WHERE s.snapshot_id=? ORDER BY s.document_id',(base['active_snapshot_id'],))]
        if not rows:raise HTTPException(409,'发布版没有资料')
        if len(rows)>50:raise HTTPException(422,'首版单知识库问答最多50份资料，请分库管理')
        return base,rows

    def create(self, base_id, key):
        self.base_scope(base_id)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM kb_conversations WHERE owner_user_id IS ? AND request_key=?',(self.owner,key)).fetchone()
            if prior:
                if prior['base_id']!=base_id:raise HTTPException(409,'请求键已用于其他知识库')
                return dict(prior)
            identifier,stamp='kconv-'+uuid4().hex,now_iso()
            db.execute('INSERT INTO kb_conversations(id,base_id,owner_user_id,title,request_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',(identifier,base_id,self.owner,'新会话',key,stamp,stamp))
        return self.require(identifier)

    def require(self, identifier):
        with self.repository.connect() as db:row=db.execute('SELECT * FROM kb_conversations WHERE id=? AND owner_user_id IS ?',(identifier,self.owner)).fetchone()
        if not row:raise HTTPException(404,'会话不存在或未获授权')
        self.access.base(row['base_id'])
        return dict(row)

    def list(self):
        with self.repository.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM kb_conversations WHERE owner_user_id IS ? ORDER BY updated_at DESC',(self.owner,))]
        result=[]
        for r in rows:
            try:self.access.base(r['base_id']);result.append(r)
            except HTTPException:pass
        return result

    def require_task(self, identifier):
        row=self.tasks.row(identifier)
        if not row:raise HTTPException(404,'任务不存在或未获授权')
        if row['kind']=='index':self.access.admin()
        elif row['owner_user_id']!=self.owner:raise HTTPException(404,'任务不存在或未获授权')
        with self.repository.connect() as db:
            if not KnowledgeTasks.allowed(db,row):raise HTTPException(404,'任务不存在或未获授权')
        return row

    def present(self, row):
        value={k:v for k,v in row.items() if k not in {'input_json','input_hash','lease_token','lease_expires_at','result_json'}}
        value['question']=json.loads(row['input_json']).get('question','')
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

    def submit(self, identifier, question, key):
        conv=self.require(identifier)
        # 请求键先复用；刷新或原请求重发不改变快照、不重新收费。
        with self.repository.connect() as db:
            prior=db.execute("SELECT j.* FROM kb_tasks j JOIN kb_conversation_turns t ON t.task_id=j.id WHERE t.conversation_id=? AND j.request_key=?",(identifier,identifier+'/'+key)).fetchone()
            if prior:
                if json.loads(prior['input_json'])['question']!=question:raise HTTPException(409,'请求键已用于不同问题')
                return self.present(self.require_task(prior['id']))
            history=[json.loads(r[0])['question'] for r in db.execute("SELECT j.input_json FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status='succeeded' AND json_extract(j.input_json,'$.memory_epoch')=? ORDER BY t.turn_no DESC LIMIT 3",(identifier,conv['memory_epoch']))][::-1] if conv['memory_enabled'] else []
        base,docs=self.base_scope(conv['base_id'])
        from app.intent_management import IntentManagement
        route=IntentManagement(self.repository).resolve(question,base['id'])
        frozen=[]
        for doc in docs:
            snapshot=self.rag.capture_snapshot(doc['document_id'],question,published=doc) if route['kind']=='KB' else None
            frozen.append({'snapshot':snapshot,'filename':doc['filename'],'document_id':doc['document_id']})
        payload={'question':question,'documents':frozen,'snapshot_id':base['active_snapshot_id'],
                 'configuration':configuration(self.access.settings,self.access.index.embedding),
                 'conversation_id':identifier,'memory_epoch':conv['memory_epoch'],'recent_questions':history,'route':route}
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
