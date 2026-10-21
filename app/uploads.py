"""共享上传存储：请求键、文档和可选会话绑定在同一事务完成。"""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
from fastapi import HTTPException
from app.file_detect import UploadFormatError, detect_format, sanitize_filename
from app.schemas import Document
from app.repository import now_iso
_SNIFF_BYTES = 1024 * 1024

def store_upload(repository, settings, file, idempotency_key, *, scope='local', link=None):
    upload_dir = settings.data_dir / "uploads"
    filename = sanitize_filename(file.filename or "")
    if idempotency_key and (len(idempotency_key) > 200 or not idempotency_key.strip()):
        raise HTTPException(422, '上传请求键无效')
    document_id = uuid4().hex
    target = upload_dir / document_id
    limit = settings.max_upload_mb * 1024 * 1024
    # 数据目录在运行期间被外部清理时，上传目录可能不存在；这里按需重建，
    # 避免返回难以理解的 500，而不是静默丢失文件。
    upload_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 先读取头部字节用于内容识别，同时统计大小；剩余内容流式写入，避免整份入内存。
        head = file.file.read(_SNIFF_BYTES)
        if len(head) > limit:
            raise UploadFormatError(413, f"文件不得超过 {settings.max_upload_mb} MB")
        size = len(head)
        # 识别需要完整内容（ZIP 目录/PDF 结构），但必须同时强制大小上限：
        # 多读 1 字节用于判断是否超限，超出即拒绝，绝不先落盘再校验。
        probe = head
        rest = b""
        if size <= limit:
            rest = file.file.read(limit - size + 1)
            size += len(rest)
            if size > limit:
                raise UploadFormatError(413, f"文件不得超过 {settings.max_upload_mb} MB")
            probe = head + rest
        detected = detect_format(filename, probe, max_pdf_pages=settings.max_pdf_pages)
        if size == 0:
            raise UploadFormatError(422, "文件为空")
        with target.open("xb") as output:
            output.write(head)
            if rest:
                output.write(rest)
        document = Document(id=document_id, filename=filename, size=size,
                            created_at=datetime.now(timezone.utc).isoformat(), status="uploaded",
                            format=detected.format, format_source=detected.source,
                            page_count=detected.page_count or 0)
        if idempotency_key:
            # 原件先写入随机路径；元数据和请求键同事务。并发复用后只删除自己的多余原件。
            fingerprint = sha256(filename.encode() + b'\0' + probe).hexdigest()
            with repository.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                previous = db.execute('SELECT * FROM document_upload_requests WHERE scope=? AND request_key=?',
                                      (scope, idempotency_key)).fetchone()
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise HTTPException(409, '该上传请求键已用于其他文件')
                    document = repository.get(previous['document_id'])
                    if document is None:
                        raise HTTPException(409, '原上传记录不可用，请联系管理员')
                    target.unlink(missing_ok=True)
                else:
                    db.execute('INSERT INTO documents(id,filename,size,created_at,status,page_count,chunk_count,error,format) '
                        'VALUES (:id,:filename,:size,:created_at,:status,:page_count,:chunk_count,:error,:format)', document.model_dump())
                    db.execute('INSERT INTO document_upload_requests VALUES (?,?,?,?,?)',
                        (scope, idempotency_key, fingerprint, document.id, now_iso()))
                if link is not None:
                    link(db, document)
        else:
            repository.create(document)
        return {"document": document, "format": detected.format,
                "format_source": detected.source, "format_note": detected.note}
    except Exception:
        # 上传校验或元数据写入失败时清理本次原件，避免残留无记录文件。
        target.unlink(missing_ok=True)
        raise
    finally:
        # 无论上传成功与否，都释放上传文件句柄。
        file.file.close()

