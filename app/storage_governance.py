"""上传写文件前持久预留容量；写入失败释放，崩溃遗留预留需明确维护。"""
import shutil
from fastapi import HTTPException
from app.product_access import actor_context
from app.repository import now_iso


def reserve(repository,settings,document_id,size):
    actor=actor_context.get();owner=actor['id'] if actor else 'local'
    if shutil.disk_usage(settings.data_dir).free<size+settings.storage_free_min_mb*1024*1024:
        raise HTTPException(507,'磁盘可用空间不足，未保存原件')
    with repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute('SELECT owner_key,byte_size FROM document_storage_owners UNION ALL SELECT owner_key,byte_size FROM storage_reservations').fetchall()
        total=sum(r['byte_size'] for r in rows);own=sum(r['byte_size'] for r in rows if r['owner_key']==owner)
        if total+size>settings.storage_global_mb*1024*1024 or own+size>settings.storage_user_mb*1024*1024:
            raise HTTPException(429,'用户或全局原件存储容量已达上限，未保存新文件')
        db.execute('INSERT INTO storage_reservations VALUES (?,?,?,?)',(document_id,owner,size,now_iso()))


def commit(db,reservation_id,document_id):
    row=db.execute('SELECT * FROM storage_reservations WHERE document_id=?',(reservation_id,)).fetchone()
    if row and reservation_id==document_id:
        db.execute('INSERT INTO document_storage_owners VALUES (?,?,?)',(document_id,row['owner_key'],row['byte_size']))
    db.execute('DELETE FROM storage_reservations WHERE document_id=?',(reservation_id,))


def release(repository,document_id):
    with repository.connect() as db:db.execute('DELETE FROM storage_reservations WHERE document_id=?',(document_id,))
