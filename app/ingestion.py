"""上传后的持久化编排；这里只衔接本地任务，不在Web或轮询里执行供应商请求。"""
import hashlib
import json
from uuid import uuid4
from fastapi import HTTPException
from app.repository import now_iso
from app.knowledge_tasks import dump,fingerprint
from app.knowledge_scope import base_allowed


def ingestion_configuration(settings,base):
    from app.embedding import APIEmbedding
    return fingerprint([APIEmbedding(settings).signature,settings.docling_base_url,settings.docling_ocr_engine,
        settings.docling_ocr_lang,settings.docling_table_mode,settings.max_pdf_pages,
        base['chunk_max_chars'],base['chunk_overlap_chars'],settings.table_rows_per_chunk,'auto-ingestion-v1'])


class ExistingIngestion(Exception):
    def __init__(self,identifier):self.identifier=identifier


class Ingestion:
    def __init__(self,access):
        self.access,self.repository,self.settings=access,access.repository,access.settings

    @property
    def owner(self):return self.access.actor['id'] if self.access.actor else None

    def require(self,identifier,*,write=False):
        with self.repository.connect() as db:
            row=db.execute('SELECT * FROM ingestion_jobs WHERE id=?',(identifier,)).fetchone()
            if not row or not base_allowed(db,row['base_id'],self.owner,write=write):raise HTTPException(404,'处理记录不存在或未获授权')
            # 普通读者不能读取未发布文件的名称、状态或错误。
            if not write and not base_allowed(db,row['base_id'],self.owner,write=True):
                from app.knowledge_scope import scope_documents
                if row['document_id'] not in scope_documents(db,row['base_id']):raise HTTPException(404,'处理记录不存在或未获授权')
            return dict(row)

    def present(self,row):
        doc=self.repository.get(row['document_id'])
        value={k:v for k,v in row.items() if k not in ('source_hash','configuration','owner_user_id','replacement_key')}
        value['filename']=doc.filename if doc else '资料'
        value['requests_used']=0;value['requests_max']=None
        if row['index_task_id']:
            with self.repository.connect() as db:
                t=db.execute('SELECT requests_used,max_requests FROM kb_tasks WHERE id=?',(row['index_task_id'],)).fetchone()
            if t:value['requests_used'],value['requests_max']=tuple(t)
        return value

    def submit(self,base_id,file,key,*,replace_document_id=None):
        base=self.access.base(base_id)
        with self.repository.connect() as db:
            if not base_allowed(db,base_id,self.owner,write=True):raise HTTPException(403,'当前知识库仅可阅读，请选择有上传权限的库或会话临时库')
            if replace_document_id and not db.execute('SELECT 1 FROM knowledge_base_documents WHERE knowledge_base_id=? AND document_id=?',(base_id,replace_document_id)).fetchone():raise HTTPException(409,'被替换文档不属于此知识库或已变更')
        content=file.file.read(self.settings.max_upload_mb*1024*1024+1)
        if len(content)>self.settings.max_upload_mb*1024*1024:
            file.file.close();raise HTTPException(413,'文件超过上传大小限制')
        file.file.seek(0)
        source_hash=hashlib.sha256(content).hexdigest();config=ingestion_configuration(self.settings,base)
        request_hash=fingerprint([file.filename,source_hash,replace_document_id]);replacement=replace_document_id or ''
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT * FROM ingestion_request_keys WHERE base_id=? AND request_key=?',(base_id,key)).fetchone()
            if previous:
                file.file.close()
                if previous['fingerprint']!=request_hash:raise HTTPException(409,'该上传请求键已用于其他文件或替换操作')
                return self.present(self.require(previous['job_id'],write=True))
            existing=db.execute('SELECT id FROM ingestion_jobs WHERE base_id=? AND source_hash=? AND configuration=? AND replacement_key=?',(base_id,source_hash,config,replacement)).fetchone()
            if existing:
                file.file.close();db.execute('INSERT INTO ingestion_request_keys VALUES (?,?,?,?)',(base_id,key,existing[0],request_hash))
                return self.present(self.require(existing[0],write=True))
        identifier='ingest-'+uuid4().hex
        def bind(db,document):
            if not base_allowed(db,base_id,self.owner,write=True):raise HTTPException(403,'账号或上传权限已变更')
            current=db.execute('SELECT * FROM knowledge_bases WHERE id=?',(base_id,)).fetchone()
            request=db.execute('SELECT * FROM ingestion_request_keys WHERE base_id=? AND request_key=?',(base_id,key)).fetchone()
            if request:
                if request['fingerprint']!=request_hash:raise HTTPException(409,'该上传请求键已用于其他文件或替换操作')
                raise ExistingIngestion(request['job_id'])
            if ingestion_configuration(self.settings,current)!=config:raise HTTPException(409,'知识库处理配置已变化，请重新提交')
            prior=db.execute('SELECT id FROM ingestion_jobs WHERE base_id=? AND source_hash=? AND configuration=? AND replacement_key=?',(base_id,source_hash,config,replacement)).fetchone()
            if prior:raise ExistingIngestion(prior[0])
            db.execute('INSERT INTO knowledge_base_documents VALUES (?,?,?)',(base_id,document.id,now_iso()))
            db.execute('INSERT INTO ingestion_jobs(id,base_id,document_id,owner_user_id,source_hash,configuration,replacement_key,replace_document_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (identifier,base_id,document.id,self.owner,source_hash,config,replacement,replace_document_id,now_iso(),now_iso()))
            db.execute('INSERT INTO ingestion_request_keys VALUES (?,?,?,?)',(base_id,key,identifier,request_hash))
        from app.uploads import store_upload
        try:store_upload(self.repository,self.settings,file,key,scope='auto-ingestion/'+base_id,link=bind)
        except ExistingIngestion as prior:
            identifier=prior.identifier
            with self.repository.connect() as db:db.execute('INSERT OR IGNORE INTO ingestion_request_keys VALUES (?,?,?,?)',(base_id,key,identifier,request_hash))
        return self.present(self.require(identifier,write=True))

    def cancel(self,identifier):
        row=self.require(identifier,write=True)
        with self.repository.connect() as db:
            db.execute("UPDATE ingestion_jobs SET status='cancelled',error_code='cancelled',error_message='已停止后续处理',updated_at=? WHERE id=? AND status!='succeeded'",(now_iso(),identifier))
            if row['parse_task_id']:db.execute("UPDATE parse_tasks SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'needs_attention' ELSE status END WHERE id=?",(row['parse_task_id'],))
            if row['index_task_id']:db.execute("UPDATE kb_tasks SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?",(row['index_task_id'],))
        return self.present(self.require(identifier,write=True))

    def retry(self,identifier,acknowledge=False):
        row=self.require(identifier,write=True)
        if row['status'] not in ('failed','needs_attention','cancelled'):raise HTTPException(409,'此处理任务无需重试')
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if row['parse_task_id'] and db.execute("SELECT 1 FROM parse_tasks WHERE id=? AND status='running'",(row['parse_task_id'],)).fetchone() or row['index_task_id'] and db.execute("SELECT 1 FROM kb_tasks WHERE id=? AND status='running'",(row['index_task_id'],)).fetchone():raise HTTPException(409,'原处理仍在停止，请等待状态收敛后重试')
            if row['index_task_id']:
                task=db.execute('SELECT * FROM kb_tasks WHERE id=?',(row['index_task_id'],)).fetchone()
                uncertain=db.execute("SELECT 1 FROM kb_task_calls WHERE task_id=? AND status IN ('intent','uncertain') AND COALESCE(error_code,'')!='retry_acknowledged'",(task['id'],)).fetchone()
                if uncertain and not acknowledge:raise HTTPException(409,'存在未确认调用，核实后明确确认可能重复计费')
                if task['requests_used']>=task['max_requests']:raise HTTPException(409,'原任务累计请求上限已用完，未自动追加额度')
                db.execute("UPDATE kb_task_calls SET status='uncertain',error_code='retry_acknowledged',finished_at=COALESCE(finished_at,?) WHERE task_id=? AND status IN ('intent','uncertain')",(now_iso(),task['id']))
                db.execute("UPDATE kb_tasks SET status='queued',cancel_requested=0,lease_token=NULL,lease_expires_at=NULL,error_code=NULL,error_message=NULL,finished_at=NULL WHERE id=?",(task['id'],))
            else:
                parse=db.execute('SELECT status,upstream_task_id FROM parse_tasks WHERE id=?',(row['parse_task_id'],)).fetchone() if row['parse_task_id'] else None
                if parse and parse['status']=='needs_attention' and not acknowledge:raise HTTPException(409,'解析提交结果不确定，请核实后明确重试')
                # 不再复用已失败解析的请求键；旧上游任务与失败记录保留。
                db.execute('UPDATE ingestion_jobs SET parse_task_id=NULL WHERE id=?',(identifier,))
            db.execute("UPDATE ingestion_jobs SET status='queued',retry_count=retry_count+1,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?",(now_iso(),identifier))
        return self.present(self.require(identifier,write=True))

    def activate(self,identifier):
        row=self.require(identifier,write=True)
        if row['status'] not in ('review','succeeded'):raise HTTPException(409,'请等待解析与索引完成')
        activate_document(self.repository,self.access.index,row)
        return self.present(self.require(identifier,write=True))


def activate_document(repository,index,row):
    """新文档单独加入当前快照；其他草稿或失败文件不会阻塞或被顺带发布。"""
    info=index.index_info(row['document_id'])
    if info['status']!='indexed' or not info['matches_active_version'] or info['parse_version_id']!=row['parse_version_id'] or info['index_id']!=row['index_id']:
        raise HTTPException(409,'已准备的文档版本发生变化，未生效')
    with repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT * FROM ingestion_jobs WHERE id=?',(row['id'],)).fetchone()
        if not current or current['status'] not in ('running','review','succeeded') or not base_allowed(db,row['base_id'],row['owner_user_id'],write=True):raise HTTPException(409,'处理任务已停止或权限已变化')
        base=db.execute('SELECT * FROM knowledge_bases WHERE id=?',(row['base_id'],)).fetchone()
        doc=db.execute('SELECT * FROM documents WHERE id=?',(row['document_id'],)).fetchone()
        if doc['active_parse_version_id']!=row['parse_version_id'] or doc['active_index_id']!=row['index_id']:raise HTTPException(409,'文档版本已变化')
        existing={r['document_id']:tuple(r) for r in db.execute('SELECT document_id,parse_version_id,index_id,origin_hash,source_signature FROM knowledge_base_snapshot_documents WHERE snapshot_id=?',(base['active_snapshot_id'],))}
        original=dict(existing)
        if row['replace_document_id']:
            if row['replace_document_id'] not in existing and row['document_id'] not in existing:raise HTTPException(409,'原文档已被移除或替换，请核对后重新提交')
            existing.pop(row['replace_document_id'],None)
        v=db.execute('SELECT origin_hash,quality_status FROM parse_versions WHERE id=?',(row['parse_version_id'],)).fetchone()
        ix=db.execute('SELECT source_signature FROM embedding_indexes WHERE id=?',(row['index_id'],)).fetchone()
        if not v or v['quality_status']=='invalid' or v['origin_hash']!=row['source_hash'] or not ix:raise HTTPException(409,'文档结构、原件或索引无效')
        candidate=(row['document_id'],row['parse_version_id'],row['index_id'],v['origin_hash'],ix[0])
        existing[row['document_id']]=candidate
        if existing!=original:
            snapshot='kbs-'+uuid4().hex;revision=base['revision']+1
            db.execute('INSERT INTO knowledge_base_snapshots VALUES (?,?,?,?,?)',(snapshot,base['id'],revision,row['owner_user_id'],now_iso()))
            for item in existing.values():db.execute('INSERT INTO knowledge_base_snapshot_documents VALUES (?,?,?,?,?,?)',(snapshot,*item))
            db.execute('UPDATE knowledge_bases SET active_snapshot_id=?,revision=?,updated_at=? WHERE id=?',(snapshot,revision,now_iso(),base['id']))
        if row['replace_document_id']:db.execute('DELETE FROM knowledge_base_documents WHERE knowledge_base_id=? AND document_id=?',(row['base_id'],row['replace_document_id']))
        db.execute("UPDATE ingestion_jobs SET status='succeeded',stage='ready',error_code=NULL,error_message=NULL,updated_at=? WHERE id=?",(now_iso(),row['id']))


class IngestionCoordinator:
    """可重复执行的短步骤。并发／崩溃通过子任务请求键和原子状态写入收敛。"""
    def __init__(self,settings,repository,*,embedding=None):
        from app.embedding import APIEmbedding
        from app.vector_index import DocumentIndex
        self.settings,self.repository=settings,repository
        self.embedding=embedding or APIEmbedding(settings);self.index=DocumentIndex(repository,self.embedding)

    def run_once(self,identifier=None):
        with self.repository.connect() as db:
            rows=db.execute("SELECT * FROM ingestion_jobs WHERE status IN ('queued','running')"+(' AND id=?' if identifier else '')+' ORDER BY updated_at LIMIT 20',(identifier,) if identifier else ()).fetchall()
        changed=False
        for value in rows:
            row=dict(value)
            try:self.step(row);changed=True
            except HTTPException as exc:self.fail(row,'failed','preparation_failed',str(exc.detail))
            except Exception:self.fail(row,'failed','preparation_failed','资料准备失败，原有可用版本保持；请查看处理详情')
        return changed

    def fail(self,row,status,code,message):
        with self.repository.connect() as db:
            changed=db.execute("UPDATE ingestion_jobs SET status=?,error_code=?,error_message=?,updated_at=? WHERE id=? AND status IN ('queued','running')",(status,code,message,now_iso(),row['id'])).rowcount
            if changed and status=='cancelled':
                if row['parse_task_id']:db.execute("UPDATE parse_tasks SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'needs_attention' ELSE status END WHERE id=?",(row['parse_task_id'],))
                if row['index_task_id']:db.execute("UPDATE kb_tasks SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?",(row['index_task_id'],))

    def step(self,row):
        from app.schemas import ParseTask
        from app.product_access import ProductAccess,actor_context,public_user
        from app.knowledge_pipeline import KnowledgePipeline
        with self.repository.connect() as db:
            if not base_allowed(db,row['base_id'],row['owner_user_id'],write=True):
                self.fail(row,'cancelled','access_revoked','账号、知识库权限或保留期限已变化');return
            base=dict(db.execute('SELECT * FROM knowledge_bases WHERE id=?',(row['base_id'],)).fetchone())
            actor=db.execute('SELECT * FROM product_users WHERE id=?',(row['owner_user_id'],)).fetchone() if row['owner_user_id'] else None
        if ingestion_configuration(self.settings,base)!=row['configuration']:
            self.fail(row,'failed','configuration_changed','处理配置已变化，原任务未继续调用，请重新提交');return
        token=actor_context.set(public_user(actor) if actor else None)
        try:
            access=ProductAccess(self.repository,self.settings,self.index)
            document=self.repository.get(row['document_id'])
            if not row['parse_task_id'] and not document.active_parse_version_id:
                key='ingest/'+row['id']+'/'+str(row['retry_count'])
                task=self.repository.find_task_by_idempotency(document.id,key) or self.repository.find_active_task(document.id)
                if not task:
                    previous=self.repository.latest_task(document.id)
                    resume=previous.upstream_task_id if row['retry_count'] and previous and previous.error_code in ('waiting_paused','lease_expired','docling_connect_failed','docling_read_timeout','docling_deadline_exceeded','local_io_error') else None
                    task=self.repository.create_task(ParseTask(id=uuid4().hex,document_id=document.id,status='queued',stage='queued',idempotency_key=key,
                        upstream_task_id=resume,request_summary={'force':False,'format':document.format,'ingestion_id':row['id']},created_at=now_iso(),updated_at=now_iso()))
                with self.repository.connect() as db:db.execute("UPDATE ingestion_jobs SET status='running',stage='parsing',parse_task_id=?,updated_at=? WHERE id=? AND status IN ('queued','running')",(task.id,now_iso(),row['id']))
                return
            if not document.active_parse_version_id:
                task=self.repository.get_task(row['parse_task_id'])
                if task and task.status in ('failed','needs_attention'):
                    self.fail(row,task.status,task.error_code or 'parse_failed',task.error_message or '解析失败')
                return
            version=self.repository.get_parse_version(document.active_parse_version_id)
            if not version or version.origin_hash!=row['source_hash'] or version.quality_status=='invalid' or not self.repository.chunks(document.id,version.id):raise HTTPException(409,'原件已变更或解析没有可用内容')
            with self.repository.connect() as db:db.execute("UPDATE ingestion_jobs SET status='running',stage='indexing',parse_version_id=?,updated_at=? WHERE id=? AND status IN ('queued','running')",(version.id,now_iso(),row['id']))
            row['parse_version_id']=version.id
            if not row['index_task_id']:
                task=KnowledgePipeline(access).submit_index(document.id,'ingest-index/'+row['id'],ingestion_id=row['id'])
                with self.repository.connect() as db:db.execute("UPDATE ingestion_jobs SET index_task_id=? WHERE id=? AND status='running'",(task['id'],row['id']))
                return
            from app.knowledge_tasks import KnowledgeTasks
            task=KnowledgeTasks(self.repository).row(row['index_task_id'])
            if not task or task['status'] in ('queued','running'):return
            if task['status']!='succeeded':self.fail(row,task['status'] if task['status'] in ('failed','needs_attention','cancelled') else 'failed',task['error_code'] or 'index_failed',task['error_message'] or '建立索引失败');return
            info=self.index.index_info(document.id)
            row['index_id']=info['index_id']
            with self.repository.connect() as db:db.execute('UPDATE ingestion_jobs SET index_id=? WHERE id=?',(row['index_id'],row['id']))
            if base['activation_policy']=='review':
                with self.repository.connect() as db:db.execute("UPDATE ingestion_jobs SET status='review',stage='review',updated_at=? WHERE id=? AND status='running'",(now_iso(),row['id']))
            else:activate_document(self.repository,self.index,row)
        finally:actor_context.reset(token)
