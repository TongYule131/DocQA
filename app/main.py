# 应用入口：装配依赖、注册 API，并提供同源静态工作台。
#
# 本阶段的主要接口变化（对应任务书工程包 E）：
# - POST /api/documents/{id}/parse 由同步改为异步：返回 202 与任务，不再阻塞等待解析；
#   已有有效解析版本且未 force 时返回 200 表示复用。
# - 新增任务查询、版本化内容预览、原件读取与解析版本级分块接口。
# - 索引固定目标解析版本，检索返回 index_id / parse_version_id / 是否旧版本与每条来源。
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from fastapi import Body, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import migrations
from app.config import Settings
from app.deepseek import DeepSeekModel, ModelError
from app.docling_client import DoclingClient
from app.embedding import APIEmbedding, EmbeddingError
from app.file_detect import FORMAT_MIME, UploadFormatError, detect_format, sanitize_filename
from app.parsing import ParseError, chunk_pages, parse_document
from app.repository import Repository, TaskConflict, now_iso
from app.schemas import (
    Answer,
    Chunk,
    Document,
    Extraction,
    IndexInfo,
    ParseRequest,
    ParseSubmitResponse,
    ParseTask,
    ParseVersion,
    Question,
    SearchRequest,
    Summary,
    UploadResponse,
)
from app.vector_index import DocumentIndex

logger = logging.getLogger(__name__)

# 上传时先读取的字节数：足够判断文件标识与 ZIP 头，避免为了识别格式把整份文件读入内存。
_SNIFF_BYTES = 1024 * 1024


def create_app(settings: Settings | None = None) -> FastAPI:
    # 应用工厂支持注入配置，测试可使用独立临时目录而不污染实际资料。
    settings = settings or Settings.from_env()
    model = DeepSeekModel(settings)
    repository = Repository(settings.data_dir / "docqa.db")
    embedding = APIEmbedding(settings)
    index = DocumentIndex(repository, embedding)
    upload_dir = settings.data_dir / "uploads"
    web_dir = Path(__file__).parent / "web"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 在服务启动阶段准备目录并执行数据库迁移；yield 后进入服务关闭阶段。
        upload_dir.mkdir(parents=True, exist_ok=True)
        try:
            summary = repository.initialize()
        except migrations.MigrationError as exc:
            # 迁移失败必须停止启动，保留原库与备份，绝不静默新建空数据库。
            logger.error("数据库迁移失败，服务停止启动：%s", exc)
            raise
        if summary.get("applied"):
            logger.info("数据库迁移完成：%s（备份 %s）", summary["applied"], summary.get("backup"))
        yield

    app = FastAPI(title="智能文档分析与知识问答系统", version="0.2.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.exception_handler(EmbeddingError)
    async def embedding_error_handler(request, exc: EmbeddingError):
        # 统一转换适配器与索引服务中的安全错误，不返回上游原始响应。
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(UploadFormatError)
    async def upload_format_error_handler(request, exc: UploadFormatError):
        # 上传识别错误：返回可读原因，不回显文件内容。
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(TaskConflict)
    async def task_conflict_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def require_document(document_id: str) -> Document:
        # 各文档接口共用存在性校验，统一返回 404。
        document = repository.get(document_id)
        if document is None:
            raise HTTPException(404, "文档不存在")
        return document

    def require_version(document_id: str, version_id: str) -> ParseVersion:
        """校验解析版本存在且属于该文档，禁止跨文档读取历史版本。"""
        version = repository.get_parse_version(version_id)
        if version is None or version.document_id != document_id:
            raise HTTPException(404, "解析版本不存在或不属于该文档")
        return version

    def require_intelligence(document_id: str):
        # 先检查解析前置条件，再明确拒绝尚未接入的智能能力，避免伪造结果。
        document = require_document(document_id)
        if document.active_parse_version_id is None:
            raise HTTPException(409, "请先成功解析文档")
        raise HTTPException(503, "文档答案生成、摘要与提取尚未接入；当前可建立向量索引并检索原文")

    @app.get("/", include_in_schema=False)
    def home():
        # HTML 和静态资源与 API 同源，前端使用相对路径调用后端。
        return FileResponse(web_dir / "index.html")

    @app.get("/api/health")
    def health():
        # 仅报告应用可响应，不代表外部模型或检索服务可用。
        return {"status": "ok", "version": "0.2.0"}

    @app.get("/api/model/status")
    def model_status():
        # 配置状态不等于连通性；读取此接口不会调用模型，也不返回密钥。
        return {"provider": "deepseek", "model": settings.deepseek_model,
                "configured": bool(settings.deepseek_api_key.strip()),
                "thinking": settings.deepseek_thinking,
                "reasoning_effort": settings.deepseek_reasoning_effort}

    @app.post("/api/model/test")
    def test_model():
        # 用户主动触发时才发送固定测试消息，不携带任何已上传的文档内容。
        try:
            answer = model.generate("你是一个连接测试助手，请简短回复。", "请回复：连接成功")
        except ModelError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        return {"provider": "deepseek", "model": settings.deepseek_model, "answer": answer}

    @app.get("/api/capabilities")
    def capabilities():
        # 能力清单反映当前实现状态；协议接口存在不等于能力已经接入。
        # 区分“格式支持”与“解析服务可达”：可达性由 /api/parsing/status 单独探测。
        return {
            "upload": True,
            "formats": {"txt": True, "pdf": True, "docx": True, "xlsx": True,
                        "doc": False, "xls": False, "pptx": False, "image": False},
            "text_pdf_parsing": True,
            "utf8_txt_parsing": True,
            "docling_parsing": True,
            "async_parse_tasks": True,
            "ocr": True,
            "ocr_note": "扫描 PDF 由 Docling + RapidOCR 处理，识别局限会在页面与结果中提示",
            "source_locations": {"pdf_page_bbox": True, "docx_section_table": True,
                                 "xlsx_sheet_cell": True, "txt_line_range": True},
            "quality_warnings": True,
            "versioned_parse": True,
            "embedding": True, "retrieval": True, "rag": False, "summary": False,
            "extraction": False, "web_crawl": False, "browser_extension": False,
            "model_adapters": {"deepseek": True, "qwen": False, "chatglm": False, "llama": False},
            "model_configured": bool(settings.deepseek_api_key.strip()),
            "embedding_configured": bool(settings.embedding_api_key.strip()),
        }

    @app.get("/api/parsing/status")
    def parsing_status():
        """解析服务可达性探测：不返回上游原始响应，不代表模型已预热或内容质量合格。"""
        client = DoclingClient(settings)
        try:
            probe = client.probe()
        finally:
            client.close()
        return {"base_url": settings.docling_base_url, "reachable": probe["reachable"],
                "http_status": probe["status_code"], "detail": probe["detail"],
                "elapsed_ms": probe["elapsed_ms"],
                "ocr_engine": settings.docling_ocr_engine, "ocr_lang": settings.docling_ocr_lang,
                "table_mode": settings.docling_table_mode,
                "max_pdf_pages": settings.max_pdf_pages,
                "note": "可达只表示 HTTP 可用；不表示模型已加载或解析内容质量合格"}

    @app.get("/api/embedding/status")
    def embedding_status():
        # 仅查看配置，不发起收费请求；维度以实际向量化结果为准。
        return {"model": settings.embedding_model, "base_url": settings.embedding_base_url,
                "configured": bool(settings.embedding_api_key.strip()),
                "batch_size": settings.embedding_batch_size}

    @app.post("/api/embedding/test")
    def test_embedding():
        # 固定短文本不含任何用户文档，展示维度和少量向量值用于确认响应格式。
        vector = embedding.embed(["这是一条文档检索连接测试文本。"])[0]
        return {"model": settings.embedding_model, "dimension": len(vector), "preview": vector[:5]}

    # ------------------------------------------------------------------
    # 上传与文档
    # ------------------------------------------------------------------
    @app.get("/api/documents", response_model=list[Document])
    def list_documents():
        # 返回工作台所需的元数据列表，正文通过分块接口单独获取。
        return repository.list_documents()

    @app.post("/api/documents", response_model=UploadResponse, status_code=201)
    def upload_document(file: UploadFile = File(...)):
        """上传文档：内容识别格式 → 大小限制 → 服务端随机 ID 落盘 → 写入元数据。

        上传不会自动建立收费索引；解析也必须由用户显式触发。
        """
        filename = sanitize_filename(file.filename or "")
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

    @app.get("/api/documents/{document_id}", response_model=Document)
    def get_document(document_id: str):
        # 查询单份文档：包含当前预览版本、可用索引版本、最近任务与质量状态。
        return require_document(document_id)

    @app.get("/api/documents/{document_id}/original")
    def get_original(document_id: str):
        """按数据库文档 ID 获取原件；不接受任意文件路径。

        PDF 允许浏览器内联查看；Office 与 TXT 作为附件下载，便于对照核对。
        """
        document = require_document(document_id)
        path = upload_dir / document_id
        if not path.exists():
            raise HTTPException(404, "原件文件不存在")
        media_type = FORMAT_MIME.get(document.format or "", "application/octet-stream")
        inline = (document.format == "pdf")
        # 文件名来自用户输入，使用 RFC 5987 编码，避免响应头注入。
        quoted = Path(document.filename).name
        disposition = "inline" if inline else "attachment"
        return FileResponse(
            path, media_type=media_type,
            headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{_quote(quoted)}"},
        )

    @app.get("/api/documents/{document_id}/versions", response_model=list[ParseVersion])
    def list_versions(document_id: str):
        # 列出该文档的全部解析版本（含旧库迁移生成的 legacy 版本）。
        require_document(document_id)
        return repository.list_parse_versions(document_id)

    @app.get("/api/documents/{document_id}/content")
    def document_content(document_id: str, version_id: str | None = None,
                         offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        """受控返回预览内容与来源；较大结果分页读取，返回稳定契约。"""
        document = require_document(document_id)
        target = version_id or document.active_parse_version_id
        if not target:
            raise HTTPException(404, "该文档尚无成功解析版本")
        version = require_version(document_id, target)
        blocks = repository.blocks(target, limit=limit, offset=offset)
        return {
            "document_id": document_id,
            "version": version,
            "is_active_version": target == document.active_parse_version_id,
            "offset": offset, "limit": limit,
            "returned": len(blocks), "total": version.block_count,
            "blocks": blocks,
        }

    @app.get("/api/documents/{document_id}/chunks", response_model=list[Chunk])
    def chunks(document_id: str, version_id: str | None = None):
        """默认返回活动解析版本的分块；可读取属于该文档的历史版本分块。

        不允许串文档读取：version_id 必须属于该文档。
        """
        document = require_document(document_id)
        if version_id is not None:
            require_version(document_id, version_id)
        return repository.chunks(document_id, version_id or document.active_parse_version_id)

    # ------------------------------------------------------------------
    # 解析任务
    # ------------------------------------------------------------------
    @app.post("/api/documents/{document_id}/parse", response_model=ParseSubmitResponse)
    def parse(document_id: str, payload: ParseRequest | None = Body(default=None),
              idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        """提交解析任务（异步）。

        - 已有有效解析版本且未 force：返回 200 与复用结果，不新建任务、不调用解析服务；
        - 新建或复用活动任务：返回 202 与 task_id；
        - 同一幂等键与相同请求返回同一任务；同键不同请求返回 409。
        """
        document = require_document(document_id)
        request = payload or ParseRequest()
        key = (request.idempotency_key or idempotency_key or "").strip() or None
        fingerprint = _request_fingerprint(request)

        if key:
            existing = repository.find_task_by_idempotency(document_id, key)
            if existing is not None:
                stored = (existing.request_summary or {}).get("fingerprint")
                if stored and stored != fingerprint:
                    raise HTTPException(409, "该幂等键已用于不同的解析请求，请更换幂等键或改用原请求参数")
                return JSONResponse(status_code=202, content=ParseSubmitResponse(
                    task=existing, document=require_document(document_id), reused=True,
                    message="已存在相同幂等键的任务，直接复用，不会重复提交上游").model_dump())

        if document.active_parse_version_id and not request.force:
            # 已有有效结果且未要求重新解析：明确复用，不调用解析服务。
            return ParseSubmitResponse(
                task=repository.latest_task(document_id), document=document, reused=True,
                message="该文档已有成功解析版本；如需重新解析请使用“重新解析”并传入 force=true")

        active = repository.find_active_task(document_id)
        if active is not None:
            # 活动任务重复点击：复用同一个任务，不产生第二次上游提交。
            return JSONResponse(status_code=202, content=ParseSubmitResponse(
                task=active, document=document, reused=True,
                message="该文档已有正在排队或执行的解析任务，已复用该任务").model_dump())

        if document.format == "txt":
            pass  # TXT 由 worker 在本地解析，不发送给 Docling。
        elif document.format is None:
            raise HTTPException(409, "无法确定该文档的格式，请重新上传")

        task = repository.create_task(ParseTask(
            id=uuid4().hex, document_id=document_id, status="queued", stage="queued",
            idempotency_key=key,
            request_summary={"force": request.force, "format": document.format,
                             "fingerprint": fingerprint},
            attempt_count=0, max_attempts=1, created_at=now_iso(), updated_at=now_iso()))
        return JSONResponse(status_code=202, content=ParseSubmitResponse(
            task=task, document=require_document(document_id), reused=False,
            message="解析任务已创建并排队；请由 worker 进程执行（python -m app.parse_worker）").model_dump())

    @app.get("/api/parse-tasks/{task_id}", response_model=ParseTask)
    def get_parse_task(task_id: str):
        """返回任务阶段、时间、告警、错误和可用结果版本。"""
        task = repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "解析任务不存在")
        if task.result_version_id:
            version = repository.get_parse_version(task.result_version_id)
            if version is not None:
                task.warnings = version.warnings
                task.stage_detail = version.quality_summary
        return task

    @app.post("/api/parse-tasks/{task_id}/retry", response_model=ParseTask)
    def retry_parse_task(task_id: str):
        """用户明确重试：新建一次任务尝试，保留原失败记录。

        用于 needs_attention（提交结果不确定）与 failed 状态；不会自动重投上游。
        """
        task = repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "解析任务不存在")
        if task.status in {"queued", "running"}:
            raise HTTPException(409, "该任务仍在排队或执行中，无需重试")
        document = require_document(task.document_id)
        # 暂停、网络中断和本地失败可复用已保存的远端 ID；远端失败/过期才新提交。
        resume_id = task.upstream_task_id if task.error_code in {
            "waiting_paused", "lease_expired", "docling_connect_failed", "docling_read_timeout",
            "docling_deadline_exceeded", "local_io_error",
        } else None
        new_task = repository.create_task(ParseTask(
            id=uuid4().hex, document_id=task.document_id, status="queued", stage="queued",
            idempotency_key=None, upstream_task_id=resume_id,
            request_summary={"force": True, "format": document.format, "retry_of": task.id,
                             "fingerprint": _request_fingerprint(ParseRequest(force=True))},
            attempt_count=0, max_attempts=1, created_at=now_iso(), updated_at=now_iso()))
        return new_task

    @app.post("/api/parse-tasks/{task_id}/cancel")
    def cancel_parse_task(task_id: str):
        """请求停止等待：worker 在轮询间隙退出并保留上游任务编号，可稍后恢复。"""
        task = repository.get_task(task_id)
        if task is None:
            raise HTTPException(404, "解析任务不存在")
        if task.status not in {"queued", "running"}:
            raise HTTPException(409, "该任务已结束，无需停止")
        repository.request_cancel(task_id)
        return {"task_id": task_id, "status": "cancel_requested",
                "message": "已请求停止；上游任务编号保留，可在稍后恢复继续"}

    # ------------------------------------------------------------------
    # 索引与检索
    # ------------------------------------------------------------------
    @app.get("/api/documents/{document_id}/index", response_model=IndexInfo)
    def index_status(document_id: str):
        """查询当前可用索引与构建尝试，并标注索引版本是否等于当前预览版本。"""
        document = require_document(document_id)
        info = index.index_info(document_id)
        return IndexInfo(
            id=info["index_id"], document_id=document_id,
            parse_version_id=info["parse_version_id"], status=info["status"],
            dimension=info["dimension"], chunk_count=info["chunk_count"], error=info["error"],
            matches_active_version=info["matches_active_version"], is_legacy=info["is_legacy"],
        )

    @app.post("/api/documents/{document_id}/index", response_model=IndexInfo)
    def build_index(document_id: str, rebuild: bool = False, version_id: str | None = None):
        """建立索引：固定目标解析版本，用户主动触发才会调用收费接口。"""
        require_document(document_id)
        if version_id is not None:
            require_version(document_id, version_id)
        index.build(document_id, rebuild=rebuild, version_id=version_id)
        info = index.index_info(document_id)
        return IndexInfo(
            id=info["index_id"], document_id=document_id,
            parse_version_id=info["parse_version_id"], status=info["status"],
            dimension=info["dimension"], chunk_count=info["chunk_count"], error=info["error"],
            matches_active_version=info["matches_active_version"], is_legacy=info["is_legacy"],
        )

    @app.get("/api/documents/{document_id}/index/attempts")
    def index_attempts(document_id: str):
        """列出索引构建尝试（含失败与过期），便于区分“本次失败”与“原索引仍可用”。"""
        require_document(document_id)
        info = index.index_info(document_id)
        return {"status": info["status"], "index_id": info["index_id"],
                "parse_version_id": info["parse_version_id"], "attempts": info["attempts"]}

    @app.post("/api/documents/{document_id}/search")
    def search_document(document_id: str, payload: SearchRequest):
        """使用当前可用索引检索，返回 index_id、parse_version_id、是否旧版本与每条来源。"""
        require_document(document_id)
        return index.search(document_id, payload.query, payload.top_k)

    # ------------------------------------------------------------------
    # 尚未接入的智能能力：明确返回未接入，不伪造结果
    # ------------------------------------------------------------------
    @app.post("/api/documents/{document_id}/summary", response_model=Summary)
    def summarize(document_id: str):
        require_intelligence(document_id)

    @app.post("/api/documents/{document_id}/extract", response_model=Extraction)
    def extract(document_id: str):
        require_intelligence(document_id)

    @app.post("/api/documents/{document_id}/questions", response_model=Answer)
    def ask(document_id: str, payload: Question):
        # FastAPI 自动校验问题；后续由 RAG 服务检索上下文并生成带引用回答。
        require_intelligence(document_id)

    return app


def _quote(value: str) -> str:
    """按 RFC 5987 对响应头文件名做百分号编码。"""
    from urllib.parse import quote
    return quote(value, safe="")


def _request_fingerprint(request: ParseRequest) -> str:
    """解析请求指纹：同键不同请求（例如 force 不同）必须能被识别为冲突。"""
    return sha256(f"force={request.force};schema=parse-v1".encode()).hexdigest()


# uvicorn app.main:app 使用的默认应用实例。
app = create_app()
