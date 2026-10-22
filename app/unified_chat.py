"""单一知识库对话入口。旧记录只读适配，新轮次全部进入库级任务。"""
import json
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from fastapi import HTTPException
from app.repository import now_iso
from app.knowledge_tasks import dump,fingerprint
from app.knowledge_scope import base_allowed,current_document


class UnifiedChat:
    def __init__(self,access,knowledge,legacy=None):
        self.access,self.knowledge,self.legacy=access,knowledge,legacy
        self.repository=access.repository

    @property
    def owner(self):return self.knowledge.owner

    def scopes(self):
        result=[]
        for base in self.access.list_bases():
            with self.repository.connect() as db:writable=base_allowed(db,base['id'],self.owner,write=True)
            detail=self.access.base_detail(base['id']);ready={r['document_id'] for r in detail['published_documents']}
            result.append({**base,'writable':writable,'documents':[{'id':d['id'],'filename':d['filename'],'ready':d['id'] in ready} for d in detail['documents']]})
        return result

    def temporary(self,key):
        owner_key=self.owner or 'local'
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT base_id FROM temporary_base_keys WHERE owner_key=? AND request_key=?',(owner_key,key)).fetchone()
            if prior:
                if not base_allowed(db,prior[0],self.owner,write=True):raise HTTPException(410,'原临时库已到期，请重新创建')
                identifier=prior[0]
            else:
                count=db.execute("SELECT COUNT(*) FROM knowledge_bases WHERE kind='temporary' AND created_by IS ? AND revoked_at IS NULL AND expires_at>?",(self.owner,now_iso())).fetchone()[0]
                if count>=self.access.settings.attachment_max_active:raise HTTPException(409,'活动临时库数量已达上限，请等待到期或移除旧库')
                identifier='kb-temp-'+uuid4().hex;stamp=now_iso()
                expires=(datetime.now(timezone.utc)+timedelta(hours=self.access.settings.attachment_ttl_hours)).isoformat()
                db.execute("INSERT INTO knowledge_bases(id,name,description,created_by,created_at,updated_at,collection_name,embedding_signature,embedding_model,kind,expires_at) VALUES (?,?,?,?,?,?,?,?,?,'temporary',?)",
                    (identifier,'会话临时资料','仅本人可见，按临时资料期限保留',self.owner,stamp,stamp,identifier.replace('-',''),self.access.index.embedding.signature,self.access.settings.embedding_model,expires))
                db.execute('INSERT INTO temporary_base_keys VALUES (?,?,?)',(owner_key,key,identifier))
        return self.access.base(identifier)

    def validate_filter(self,base_id,document_ids):
        self.access.base(base_id)
        with self.repository.connect() as db:
            write=base_allowed(db,base_id,self.owner,write=True)
            allowed={r[0] for r in db.execute('SELECT document_id FROM knowledge_base_snapshot_documents WHERE snapshot_id=(SELECT active_snapshot_id FROM knowledge_bases WHERE id=?)',(base_id,))}
            if write:allowed|={r[0] for r in db.execute('SELECT document_id FROM knowledge_base_documents WHERE knowledge_base_id=?',(base_id,))}
            if any(current_document(db,base_id,i) not in allowed for i in document_ids):raise HTTPException(404,'所选资料不在可访问的知识库范围')

    def create(self,base_id,key,document_ids):
        self.validate_filter(base_id,document_ids)
        row=self.knowledge.create(base_id,key,allow_preparing=True,document_ids=document_ids)
        return self.detail('k:'+row['id'])

    def original(self,identifier):
        source,sep,raw=identifier.partition(':')
        if not sep or source not in ('k','d','q'):raise HTTPException(404,'会话不存在')
        if source=='k':return source,self.knowledge.require(raw)
        if not self.legacy:raise HTTPException(404,'历史入口不可用')
        if source=='d':return source,self.legacy.require(raw)
        self.access.require_question(raw)
        row=self.legacy.questions.store.row(raw)
        if row['conversation_id']:raise HTTPException(404,'该记录已归属会话')
        return source,row

    def linked(self,source,raw):
        if source=='k':return raw
        with self.repository.connect() as db:
            row=db.execute('SELECT conversation_id FROM unified_chat_links WHERE source=? AND source_id=?',('document' if source=='d' else 'single',raw)).fetchone()
        return row[0] if row else None

    def legacy_base(self,document_id):
        with self.repository.connect() as db:
            row=db.execute('SELECT base_id FROM legacy_document_bases WHERE document_id=?',(document_id,)).fetchone()
            if not row:row=db.execute('SELECT knowledge_base_id FROM knowledge_base_documents WHERE document_id=?',(document_id,)).fetchone()
        return row[0] if row else None

    def list(self,archived=False):
        with self.repository.connect() as db:linked={r[0] for r in db.execute('SELECT conversation_id FROM unified_chat_links')}
        rows=[]
        for row in self.knowledge.list():
            if row['id'] not in linked and bool(row['archived'])==archived:rows.append({**row,'id':'k:'+row['id']})
        if self.legacy:
            for row in self.legacy.list(archived):
                linked_id=self.linked('d',row['id'])
                if linked_id:
                    try:
                        canonical=self.knowledge.require(linked_id)
                        if bool(canonical['archived'])!=archived:continue
                        row={**row,'title':canonical['title'],'updated_at':canonical['updated_at']}
                    except HTTPException:continue
                rows.append({**row,'id':'d:'+row['id'],'base_id':self.legacy_base(row['document_id'])})
            with self.repository.connect() as db:
                jobs=db.execute('SELECT * FROM question_jobs WHERE conversation_id IS NULL AND (owner_user_id IS ? OR owner_user_id IS NULL) ORDER BY created_at DESC',(self.owner,)).fetchall()
            for job in jobs:
                try:
                    self.access.require_question(job['id']);linked_id=self.linked('q',job['id'])
                    canonical=self.knowledge.require(linked_id) if linked_id else None
                    if bool(canonical['archived'] if canonical else False)!=archived:continue
                    rows.append({'id':'q:'+job['id'],'base_id':self.legacy_base(job['document_id']),'title':canonical['title'] if canonical else job['question'][:60],
                        'updated_at':canonical['updated_at'] if canonical else job['created_at']})
                except HTTPException:pass
        return sorted(rows,key=lambda r:r['updated_at'],reverse=True)

    def detail(self,identifier):
        source,original=self.original(identifier);raw=identifier.split(':',1)[1]
        canonical_id=self.linked(source,raw);canonical=self.knowledge.detail(canonical_id) if canonical_id else None
        turns=[]
        if source!='k':
            if source=='d':old=self.legacy.detail(raw)['turns']
            else:
                job=self.legacy.questions.get_job(raw)
                old=[{'job':job,'result':self.legacy.questions.get_result(job['result_id']) if job['result_id'] else None}]
            for item in old:
                job=item['job'];turns.append({**job,'source':'question','result':item['result']['answer'] if item['result'] else None,
                    'export_url':f"/api/question-results/{job['result_id']}/export?format=json" if job['result_id'] else None})
        if canonical:
            turns += [{**t,'source':'knowledge','export_url':f"/api/kb-tasks/{t['id']}/export"} for t in canonical['turns']]
        base_id=canonical['base_id'] if canonical else self.legacy_base(original['document_id'])
        base=self.access.base(base_id) if base_id else None
        document_ids=json.loads(canonical['document_filter_json']) if canonical else [original['document_id']]
        with self.repository.connect() as db:
            waiting=[dict(r) for r in db.execute('SELECT * FROM chat_submissions WHERE conversation_id=? ORDER BY created_at',(canonical_id,))] if canonical_id else []
        return {'id':identifier,'canonical_id':canonical_id,'base_id':base_id,'base':base,'title':canonical['title'] if canonical else original.get('title',original.get('question','历史问答')),
            'revision':canonical['revision'] if canonical else original.get('revision',0),'archived':canonical['archived'] if canonical else original.get('archived',0),
            'memory_enabled':canonical['memory_enabled'] if canonical else original.get('memory_enabled',True),
            'memory_notes':canonical['memory_notes'] if canonical else original.get('memory_notes',''),
            'document_ids':document_ids,'turns':turns,'submissions':waiting}

    def bridge(self,identifier):
        source,original=self.original(identifier);raw=identifier.split(':',1)[1]
        linked=self.linked(source,raw)
        if linked:return linked
        base_id=self.legacy_base(original['document_id'])
        if not base_id:raise HTTPException(409,'历史资料尚未加入知识库，请先由管理员归库；原回答仍可查看')
        self.access.base(base_id)
        row=self.knowledge.create(base_id,'legacy/'+identifier,allow_preparing=True,document_ids=[original['document_id']])
        memory=self.legacy.memory(original)['context'] if source=='d' else {'recent_questions':[original['question'][:600]] if original['status']=='succeeded' else [],'notes':''}
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT conversation_id FROM unified_chat_links WHERE source=? AND source_id=?',('document' if source=='d' else 'single',raw)).fetchone()
            if prior:return prior[0]
            db.execute('INSERT INTO unified_chat_links VALUES (?,?,?)',('document' if source=='d' else 'single',raw,row['id']))
            db.execute('UPDATE kb_conversations SET title=?,memory_enabled=?,memory_notes=?,initial_context_json=?,archived=? WHERE id=?',
                (original.get('title',original.get('question','历史问答'))[:80],original.get('memory_enabled',1),memory['notes'],dump(memory['recent_questions']),original.get('archived',0),row['id']))
        return row['id']

    def update(self,identifier,body):
        before=self.detail(identifier)
        if before['revision']!=body.expected_revision:raise HTTPException(409,'会话已变更，请刷新')
        canonical_id=self.bridge(identifier)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE');conv=self.knowledge.require(canonical_id)
            # 首次桥接时沿用旧会话的并发版本；后续均检查新版本。
            if before['canonical_id'] and conv['revision']!=body.expected_revision:raise HTTPException(409,'会话已变更，请刷新')
            memory_change=body.clear_memory or body.memory_enabled is not None or body.memory_notes is not None
            if memory_change and db.execute("SELECT 1 FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status IN ('queued','running')",(canonical_id,)).fetchone():raise HTTPException(409,'请先停止本轮再修改记忆')
            if memory_change or body.archived or body.deleted:
                db.execute("UPDATE chat_submissions SET status='cancelled',error_message='会话或记忆已变化，请重新提交',updated_at=? WHERE conversation_id=? AND status='waiting'",(now_iso(),canonical_id))
            if body.archived or body.deleted:
                db.execute("UPDATE kb_tasks SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id IN (SELECT task_id FROM kb_conversation_turns WHERE conversation_id=?) AND status IN ('queued','running')",(canonical_id,))
            db.execute('UPDATE kb_conversations SET title=?,memory_enabled=?,memory_notes=?,memory_epoch=memory_epoch+?,initial_context_json=?,archived=?,deleted_at=?,revision=revision+1,updated_at=? WHERE id=?',
                (body.title or conv['title'],int(body.memory_enabled) if body.memory_enabled is not None else conv['memory_enabled'],
                 '' if body.clear_memory else body.memory_notes if body.memory_notes is not None else conv['memory_notes'],int(memory_change),
                 '[]' if memory_change else conv['initial_context_json'],int(body.archived) if body.archived is not None else conv['archived'],now_iso() if body.deleted else None,now_iso(),canonical_id))
        if identifier.startswith('d:') and (body.archived is not None or body.deleted):
            from app.conversations import stop_conversation_jobs
            with self.repository.connect() as db:
                if body.archived or body.deleted:stop_conversation_jobs(db,identifier[2:])
                db.execute('UPDATE conversations SET archived=?,deleted_at=? WHERE id=?',(int(body.archived) if body.archived is not None else before['archived'],now_iso() if body.deleted else None,identifier[2:]))
        return {'deleted':True} if body.deleted else self.detail(identifier)

    def submit(self,identifier,question,key,document_ids=None):
        source,original=self.original(identifier)
        with self.repository.connect() as db:
            if source=='d' and db.execute("SELECT 1 FROM question_jobs WHERE conversation_id=? AND status IN ('queued','running')",(original['id'],)).fetchone() or source=='q' and original['status'] in ('queued','running'):raise HTTPException(409,'原会话仍有活动任务，请等待完成或停止')
        canonical_id=self.bridge(identifier);conv=self.knowledge.require(canonical_id)
        if conv['archived']:raise HTTPException(409,'会话已归档')
        ids=json.loads(conv['document_filter_json']) if document_ids is None else document_ids
        signature=fingerprint([question,ids])
        with self.repository.connect() as db:
            prior=db.execute('SELECT * FROM chat_submissions WHERE conversation_id=? AND request_key=?',(canonical_id,key)).fetchone()
        if prior:
            if prior['fingerprint']!=signature:raise HTTPException(409,'请求键已用于不同问题或范围')
            return dict(prior)
        self.validate_filter(conv['base_id'],ids)
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM chat_submissions WHERE conversation_id=? AND request_key=?',(canonical_id,key)).fetchone()
            if prior:
                if prior['fingerprint']!=signature:raise HTTPException(409,'请求键已用于不同问题或范围')
                return dict(prior)
            if db.execute("SELECT 1 FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status IN ('queued','running')",(canonical_id,)).fetchone():raise HTTPException(409,'本会话已有活动任务')
            if db.execute("SELECT 1 FROM chat_submissions WHERE conversation_id=? AND status='waiting'",(canonical_id,)).fetchone():raise HTTPException(409,'本会话已有等待资料的问题')
            base=db.execute('SELECT * FROM knowledge_bases WHERE id=?',(conv['base_id'],)).fetchone()
            jobs=[dict(r) for r in db.execute('SELECT * FROM ingestion_jobs WHERE base_id=?',(conv['base_id'],))]
            if ids:
                wanted={current_document(db,conv['base_id'],i) for i in ids}
                waits=[j['id'] for j in jobs if j['document_id'] in wanted]
            else:
                waits=[j['id'] for j in jobs if j['status'] in ('queued','running','review')] if not base['active_snapshot_id'] else []
            submission='submission-'+uuid4().hex;stamp=now_iso()
            db.execute('INSERT INTO chat_submissions(id,conversation_id,owner_user_id,request_key,fingerprint,question,document_ids_json,waiting_jobs_json,memory_epoch,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (submission,canonical_id,self.owner,key,signature,question,dump(ids),dump(waits),conv['memory_epoch'],stamp,stamp))
            db.execute("UPDATE kb_conversations SET title=CASE WHEN title='新会话' THEN ? ELSE title END,updated_at=? WHERE id=?",(question[:60],stamp,canonical_id))
        # 仅创建/衔接本地持久化任务；所有供应商调用仍由worker执行。
        ChatSubmissionCoordinator(self.access.settings,self.repository,knowledge=self.knowledge).run_once(submission)
        with self.repository.connect() as db:return dict(db.execute('SELECT * FROM chat_submissions WHERE id=?',(submission,)).fetchone())


class ChatSubmissionCoordinator:
    def __init__(self,settings,repository,*,knowledge=None):
        self.settings,self.repository,self.knowledge=settings,repository,knowledge

    def run_once(self,identifier=None):
        with self.repository.connect() as db:
            rows=db.execute("SELECT * FROM chat_submissions WHERE status='waiting'"+(' AND id=?' if identifier else '')+' ORDER BY created_at LIMIT 20',(identifier,) if identifier else ()).fetchall()
        for item in rows:
            row=dict(item)
            try:self.step(row)
            except HTTPException as exc:self.finish(row,'failed','scope_unavailable',str(exc.detail))
            except Exception:self.finish(row,'failed','submission_failed','无法提交本轮，请刷新核对资料与会话')

    def finish(self,row,status,code,message,task=None):
        with self.repository.connect() as db:db.execute("UPDATE chat_submissions SET status=?,error_code=?,error_message=?,task_id=?,updated_at=? WHERE id=? AND status='waiting'",(status,code,message,task,now_iso(),row['id']))

    def step(self,row):
        from app.product_access import ProductAccess,actor_context,public_user
        with self.repository.connect() as db:
            conv=db.execute('SELECT * FROM kb_conversations WHERE id=?',(row['conversation_id'],)).fetchone()
            if not conv or conv['deleted_at'] or conv['archived'] or conv['memory_epoch']!=row['memory_epoch'] or not base_allowed(db,conv['base_id'],row['owner_user_id']):
                self.finish(row,'cancelled','access_changed','会话、记忆或资料权限已变化');return
            for identifier in json.loads(row['waiting_jobs_json']):
                job=db.execute('SELECT * FROM ingestion_jobs WHERE id=?',(identifier,)).fetchone()
                if not job or job['status'] in ('failed','needs_attention','cancelled'):
                    self.finish(row,'failed','document_not_ready','所选文件准备失败或已停止，未改用其他资料回答');return
                if job['status']!='succeeded':return
            actor=db.execute('SELECT * FROM product_users WHERE id=?',(row['owner_user_id'],)).fetchone() if row['owner_user_id'] else None
        token=actor_context.set(public_user(actor) if actor else None)
        try:
            knowledge=self.knowledge
            if knowledge is None:
                from app.rag import RagService
                from app.embedding import APIEmbedding
                from app.vector_index import DocumentIndex
                from app.deepseek import DeepSeekModel
                from app.knowledge_questions import KnowledgeQuestions
                index=DocumentIndex(self.repository,APIEmbedding(self.settings));access=ProductAccess(self.repository,self.settings,index)
                knowledge=KnowledgeQuestions(access,RagService(self.settings,self.repository,index,DeepSeekModel(self.settings)))
            task=knowledge.submit(row['conversation_id'],row['question'],'submission/'+row['id'],document_ids=json.loads(row['document_ids_json']),submission_id=row['id'])
            self.finish(row,'submitted',None,None,task['id'])
        finally:actor_context.reset(token)
