"""知识库配置、索引任务提交及保留不可变来源的分块修订。"""
import json
import math
from uuid import uuid4

from fastapi import HTTPException

from app.knowledge_tasks import KnowledgeTasks, fingerprint, configuration, dump
from app.repository import now_iso
from app.vector_index import evidence_signature


class KnowledgePipeline:
    def __init__(self, access):
        self.access,self.repository,self.settings=access,access.repository,access.settings
        self.tasks=KnowledgeTasks(self.repository)

    def submit_index(self, document_id, key, *, rebuild=False, ingestion_id=None):
        if ingestion_id:
            from app.ingestion import Ingestion, ingestion_configuration
            ingestion=Ingestion(self.access).require(ingestion_id,write=True)
            base=self.access.base(ingestion['base_id'])
            if ingestion['document_id']!=document_id or ingestion['status'] not in ('queued','running') or ingestion['configuration']!=ingestion_configuration(self.settings,base):
                raise HTTPException(409,'上传任务已停止或处理配置已变化')
        else:
            self.access.admin();self.access.require_document(document_id)
            if self.access.attachment(document_id):raise HTTPException(409,'临时资料请在所属会话准备')
        document=self.repository.get(document_id)
        if not document or not document.active_parse_version_id:raise HTTPException(409,'请先完成文档解析')
        chunks=self.repository.chunks(document_id,document.active_parse_version_id)
        if not chunks:raise HTTPException(409,'此版本没有可索引内容')
        with self.repository.connect() as db:
            base=db.execute('SELECT k.* FROM knowledge_bases k JOIN knowledge_base_documents d ON d.knowledge_base_id=k.id WHERE d.document_id=?',(document_id,)).fetchone()
            if base and base['embedding_signature'] and base['embedding_signature']!=self.access.index.embedding.signature:
                raise HTTPException(409,'知识库向量模型与当前配置不一致，请恢复配置或迁移到新知识库')
            if base and not base['embedding_signature']:
                db.execute('UPDATE knowledge_bases SET embedding_signature=?,embedding_model=? WHERE id=?',(self.access.index.embedding.signature,self.settings.embedding_model,base['id']))
        payload={'version_id':document.active_parse_version_id,'evidence_signature':evidence_signature(chunks),
                 'configuration':configuration(self.settings,self.access.index.embedding),'rebuild':rebuild}
        if ingestion_id:payload['ingestion_id']=ingestion_id
        maximum=math.ceil(len(chunks)/self.settings.embedding_batch_size)
        if maximum>256:raise HTTPException(422,'单次索引超过256批，请拆分资料')
        # 明确重试沿用同一账本及批次检查点；从不自动重发失败或不确定的调用。
        if ingestion_id:maximum=min(256,maximum*2)
        return self.tasks.create('index',payload,key,base_id=base['id'] if base else None,document_id=document_id,
            owner=self.access.actor['id'] if self.access.actor else None,max_requests=maximum)

    def chunk_detail(self, document_id, version_id=None):
        self.access.admin();self.access.require_document(document_id)
        document=self.repository.get(document_id)
        if not document:raise HTTPException(404,'资料不存在')
        version_id=version_id or document.active_parse_version_id
        version=self.repository.get_parse_version(version_id) if version_id else None
        if not version or version.document_id!=document_id:raise HTTPException(404,'解析版本不存在')
        return {'document':document.model_dump(),'version':version.model_dump(),
                'chunks':[c.model_dump() for c in self.repository.chunks(document_id,version_id)],
                'versions':[v.model_dump() for v in self.repository.list_parse_versions(document_id)]}

    def revise(self, document_id, version_id, edits, reason):
        """编辑只调整原文连续范围或拆分/合并；文字纠错须由新原件进入解析链。"""
        detail=self.chunk_detail(document_id,version_id)
        if self.access.attachment(document_id):raise HTTPException(409,'临时资料不支持后台修订')
        if detail['document']['active_parse_version_id']!=version_id:raise HTTPException(409,'预览版本已变化，请刷新')
        originals=self.repository.chunks(document_id,version_id)
        if not 1<=len(edits)<=2000:raise HTTPException(422,'分块数量必须为1～2000')
        by_id={c.id:c for c in originals};positions={c.id:n for n,c in enumerate(originals)};output=[];used=[]
        for edit in edits:
            ids=edit['chunk_ids']
            if not ids or any(identifier not in by_id for identifier in ids):raise HTTPException(422,'分块来源不属于所选版本')
            order=[positions[i] for i in ids]
            if order!=list(range(order[0],order[0]+len(order))):raise HTTPException(422,'只能合并相邻分块')
            joined='\n'.join(by_id[i].text for i in ids)
            a,b=edit['start'],edit['end']
            if not 0<=a<b<=len(joined):raise HTTPException(422,'原文范围无效')
            if b-a>16000:raise HTTPException(422,'单个分块不得超过16000字符')
            used.append((order[0],a,order[-1],b))
            sources=[]
            for identifier in ids:
                for source in by_id[identifier].sources:
                    if source not in sources:sources.append(source)
            first=by_id[ids[0]]
            output.append(first.model_copy(update={'id':'chunk-'+uuid4().hex,'text':joined[a:b],
                'sources':sources,'order_index':len(output),'chunk_type':first.chunk_type,
                'block_id':first.block_id if len(ids)==1 else None}))
        # 必须覆盖所有原文，不允许因编辑遗漏政策条件；拆分范围连续且不重叠。
        rebuilt=''.join(c.text for c in output).replace('\r\n','\n')
        original=''.join(c.text for c in originals).replace('\r\n','\n')
        if ''.join(rebuilt.split())!=''.join(original.split()):
            raise HTTPException(422,'调整必须保留全部原文和顺序；请用拆分/合并调整边界，文字修订请上传新版原件')
        identifier='v-chunks-'+uuid4().hex;stamp=now_iso()
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT active_parse_version_id FROM documents WHERE id=?',(document_id,)).fetchone()
            if current[0]!=version_id:raise HTTPException(409,'文档版本已变化')
            parent=dict(db.execute('SELECT * FROM parse_versions WHERE id=?',(version_id,)).fetchone())
            parent.update(id=identifier,task_id=None,result_hash=fingerprint([version_id,edits]),created_at=stamp,
                parser_name='chunk-boundary-editor',chunk_count=len(output),config_summary=dump({'parent':version_id,'reason':reason}))
            self._insert(db,'parse_versions',parent)
            block_map={}
            for row in db.execute('SELECT * FROM blocks WHERE parse_version_id=?',(version_id,)).fetchall():
                block=dict(row);block_map[block['id']]=identifier+'-'+block['id'];block.update(id=block_map[block['id']],parse_version_id=identifier)
                self._insert(db,'blocks',block)
            for table in ['sources','quality_warnings']:
                for row in db.execute(f'SELECT * FROM {table} WHERE parse_version_id=?',(version_id,)).fetchall():
                    data=dict(row);data.update(id=identifier+'-'+data['id'],parse_version_id=identifier)
                    if data.get('block_id'):data['block_id']=block_map.get(data['block_id'])
                    self._insert(db,table,data)
            for c in output:
                db.execute('INSERT INTO chunks(id,document_id,page,text,parse_version_id,block_id,order_index,chunk_type,sources_json,char_count) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (c.id,document_id,c.page,c.text,identifier,block_map.get(c.block_id),c.order_index,c.chunk_type,dump([s.model_dump() for s in c.sources]),len(c.text)))
            db.execute('INSERT INTO kb_chunk_revisions VALUES (?,?,?,?,?,?)',(identifier,version_id,document_id,self.access.actor['id'] if self.access.actor else None,reason,stamp))
            db.execute('UPDATE documents SET active_parse_version_id=?,chunk_count=? WHERE id=?',(identifier,len(output),document_id))
        return self.chunk_detail(document_id,identifier)

    @staticmethod
    def _insert(db, table, data):
        db.execute(f'INSERT INTO {table} ({",".join(data)}) VALUES ({",".join("?" for _ in data)})',tuple(data.values()))
