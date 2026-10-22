"""Web和后台任务共用的知识库权限、临时资料期限及版本替换范围。"""
from app.repository import now_iso


def base_allowed(db,base_id,owner,*,write=False):
    base=db.execute('SELECT * FROM knowledge_bases WHERE id=?',(base_id,)).fetchone()
    if not base or base['revoked_at'] or base['expires_at'] and base['expires_at']<=now_iso():return False
    user=db.execute('SELECT role,active FROM product_users WHERE id=?',(owner,)).fetchone() if owner else None
    if owner and (not user or not user['active']):return False
    if base['kind']=='temporary':
        if base['created_by']!=owner:return False
        # 迁移的旧附件仍可从兼容入口移除；两个入口必须共享撤销语义。
        invalid=db.execute('SELECT 1 FROM knowledge_base_documents d JOIN conversation_attachments a ON a.document_id=d.document_id JOIN conversations c ON c.id=a.conversation_id WHERE d.knowledge_base_id=? AND (a.revoked_at IS NOT NULL OR a.purged_at IS NOT NULL OR a.expires_at<=? OR c.deleted_at IS NOT NULL)',(base_id,now_iso())).fetchone()
        return not invalid
    admin=owner is None or user['role']=='admin'
    if base['kind']=='legacy' or write:return admin
    return admin or bool(db.execute('SELECT 1 FROM knowledge_base_members WHERE knowledge_base_id=? AND user_id=?',(base_id,owner)).fetchone())


def scope_documents(db,base_id):
    """当前发布文档与其替换历史；移除当前文档后历史版本也退出可读范围。"""
    ids={r[0] for r in db.execute('SELECT d.document_id FROM knowledge_base_snapshot_documents d JOIN knowledge_bases k ON k.active_snapshot_id=d.snapshot_id WHERE k.id=?',(base_id,))}
    while True:
        more={r['replace_document_id'] for r in db.execute("SELECT document_id,replace_document_id FROM ingestion_jobs WHERE base_id=? AND status='succeeded' AND replace_document_id IS NOT NULL",(base_id,)) if r['document_id'] in ids}
        if more<=ids:return ids
        ids|=more


def current_document(db,base_id,document_id):
    seen=set()
    while document_id not in seen:
        seen.add(document_id)
        row=db.execute("SELECT document_id FROM ingestion_jobs WHERE base_id=? AND replace_document_id=? AND status='succeeded' ORDER BY updated_at DESC LIMIT 1",(base_id,document_id)).fetchone()
        if not row:return document_id
        document_id=row[0]
    return document_id
