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

from fastapi import Body, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler

from app import migrations
from app.analysis_jobs import AnalysisJobService
from app.analysis_prompts import EXTRACTION_PROMPT_VERSION, SUMMARY_PROMPT_VERSION
from app.answer_stream import STREAM_VERSION
from app.config import Settings
from app.deepseek import DeepSeekModel, ModelError
from app.docling_client import DoclingClient
from app.embedding import APIEmbedding, EmbeddingError
from app.file_detect import FORMAT_MIME, UploadFormatError, detect_format, sanitize_filename
from app.parsing import ParseError, chunk_pages, parse_document
from app.rag import RagError, RagService
from app.question_jobs import QuestionJobService
from app.rag_prompts import PROMPT_VERSION, TASK_PROMPT_VERSION
from app.rag_answer_contract import ANSWER_CONTRACT_VERSION
from app.repository import Repository, TaskConflict, now_iso
from app.runtime import RuntimeGuard, RuntimeMismatch
from app.product_access import COOKIE, ProductAccess, actor_context
from app.product_routes import authorize_request, install_product_routes, verify_mutation, protect_temporary_request
from app.admin_routes import install_admin_routes
from app.profile import install_profile_routes
from app.conversations import Conversations
from app.conversation_routes import install_conversation_routes
from app.schemas import (
    AnalysisJob,
    AnalysisJobRequest,
    AnalysisPlan,
    AnalysisPlanRequest,
    AnalysisResult,
    AnalysisResultSummary,
    Answer,
    Chunk,
    Document,
    IndexInfo,
    ParseRequest,
    ParseSubmitResponse,
    ParseTask,
    ParseVersion,
    Question,
    QuestionJobRequest,
    QuestionRetryRequest,
    RagAnswer,
    SearchRequest,
    UploadResponse,
)
from app.vector_index import DocumentIndex

logger = logging.getLogger(__name__)

# 上传时先读取的字节数：足够判断文件标识与 ZIP 头，避免为了识别格式把整份文件读入内存。
_SNIFF_BYTES = 1024 * 1024


def create_app(settings: Settings | None = None) -> FastAPI:
    # 应用工厂支持注入配置，测试可使用独立临时目录而不污染实际资料。
    settings = settings or Settings.from_env()
    from app.role_logging import configure
    configure(settings.data_dir,'web')
    runtime = RuntimeGuard(settings.data_dir, "web")
    model = DeepSeekModel(settings)
    repository = Repository(settings.data_dir / "docqa.db")
    repository.settings = settings
    embedding = APIEmbedding(settings)
    index = DocumentIndex(repository, embedding)
    access = ProductAccess(repository, settings, index)
    # RAG 服务在应用装配阶段构造一次；每次提问只调用一次查询 embedding 与一次生成。
    rag = RagService(settings, repository, index, model)
    # 分析任务服务只做规划与任务生命周期；生成由独立 worker 进程执行。
    analysis = AnalysisJobService(settings, repository)
    questions = QuestionJobService(settings, repository, rag)
    conversations = Conversations(access, questions)
    upload_dir = settings.data_dir / "uploads"
    web_dir = Path(__file__).parent / "web"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 在服务启动阶段准备目录并执行数据库迁移；yield 后进入服务关闭阶段。
        runtime.prepare_database(settings.allow_database_upgrade)
        upload_dir.mkdir(parents=True, exist_ok=True)
        try:
            summary = repository.initialize()
        except migrations.MigrationError as exc:
            # 迁移失败必须停止启动，保留原库与备份，绝不静默新建空数据库。
            logger.error("数据库迁移失败，服务停止启动：%s", exc)
            raise
        if summary.get("applied"):
            logger.info("数据库迁移完成：%s（备份 %s）", summary["applied"], summary.get("backup"))
        runtime.mark_ready()
        import asyncio
        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                try:runtime.check()
                except RuntimeMismatch:return
        heartbeat_task=asyncio.create_task(heartbeat())
        try:yield
        finally:
            heartbeat_task.cancel()
            try:await heartbeat_task
            except asyncio.CancelledError:pass
            runtime.release_lifecycle()

    app = FastAPI(title="智能文档分析与知识问答系统", version="0.2.0", lifespan=lifespan)
    app.state.runtime_guard = runtime
    app.state.question_service = questions
    app.state.product_access = access
    app.state.conversations = conversations
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.exception_handler(RequestValidationError)
    async def input_error(request, exc):
        if request.url.path.startswith(('/api/auth/', '/api/admin/users')):
            # 账号表单错误不能通过默认 validation input 字段回显口令。
            return JSONResponse(status_code=422, content={'detail': '账号资料格式无效，请检查各项长度和类型'})
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def require_compatible_runtime(request, call_next):
        request.state.request_id = f"req-{uuid4().hex}"
        page_route = request.url.path in {"/", "/app", "/single", "/admin", "/tools", "/knowledge", "/login", "/setup",'/account/password'} or request.url.path.startswith(('/admin/','/app/'))
        # 代码更新或库被外部替换后，旧进程不得继续提交/领取收费任务。
        if page_route or request.url.path.startswith(("/api/", "/static/")):
            try:
                runtime.check()
            except RuntimeMismatch as exc:
                return JSONResponse(status_code=503, content={
                    "status": "not_ready", "code": "runtime_mismatch", "detail": str(exc)})
        actor = access.session(request.cookies.get(access.cookie_name)) if access.team_mode else None
        token = actor_context.set(actor)
        try:
            path = request.url.path
            from app.account_security import restrict_password_change,guard_session_stream
            if access.team_mode and restrict_password_change(request,actor):
                if page_route:return RedirectResponse('/account/password',status_code=303,headers={'Cache-Control':'no-store'})
                return JSONResponse(status_code=403,content={'code':'password_change_required','detail':'请先修改初始密码后再使用业务功能'},headers={'Cache-Control':'no-store'})
            if access.team_mode and not path.startswith('/static/') and path not in ('/login', '/setup', '/api/auth/login', '/api/auth/me', '/api/auth/setup-status', '/api/auth/setup'):
                if not actor:
                    if page_route:
                        return RedirectResponse('/login', status_code=303, headers={'Cache-Control': 'no-store'})
                    raise HTTPException(401, '请先登录')
                authorize_request(access, request)
            if access.team_mode:
                verify_mutation(request, actor)
            protect_temporary_request(access, request)
            response = await call_next(request)
            if access.team_mode and actor and response.headers.get('content-type','').startswith('text/event-stream'):
                guard_session_stream(response,access,request.cookies.get(access.cookie_name),actor['id'])
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
        finally:
            actor_context.reset(token)
        response.headers["X-Request-ID"] = request.state.request_id
        if page_route or request.url.path.startswith(("/static/", "/api/")):
            # 本地升级后普通刷新也必须拿到当前页面，不能继续复用旧 JS/CSS。
            response.headers["Cache-Control"] = "no-store"
        return response

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

    @app.exception_handler(RagError)
    async def rag_error_handler(request, exc: RagError):
        """RAG 业务错误：保留字符串 detail，并附带稳定错误码。

        不返回上游响应正文、异常堆栈、密钥或完整 Prompt。
        """
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": str(exc), "code": exc.code})

    @app.exception_handler(ModelError)
    async def model_error_handler(request, exc: ModelError):
        """生成阶段的上游错误：沿用现有安全 ModelError 映射并补稳定错误码。

        超时映射为 504，限流映射为 503，其余上游错误为 502；响应不含上游正文。
        """
        code = {504: "model_timeout", 503: "model_unavailable"}.get(exc.status_code,
                                                                   "model_error")
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": str(exc), "code": code})

    def require_document(document_id: str) -> Document:
        # 各文档接口共用存在性校验，统一返回 404。
        access.require_document(document_id)
        document = repository.get(document_id)
        if document is None:
            raise HTTPException(404, "文档不存在")
        return document

    def require_version(document_id: str, version_id: str) -> ParseVersion:
        """校验解析版本存在且属于该文档，禁止跨文档读取历史版本。"""
        version = repository.get_parse_version(version_id)
        if version is None or version.document_id != document_id:
            raise HTTPException(404, "解析版本不存在或不属于该文档")
        if access.team_mode and access.actor['role'] != 'admin':
            published = access.require_document(document_id)
            if published and version_id != published['parse_version_id'] and not access.owns_source_version(document_id,version_id):
                raise HTTPException(404, '解析版本未发布或未获授权')
        return version

    def workspace_page(filename: str, view: str = ""):
        # 文件名仅来自下面固定路由，绝不接受任意路径。模块资源也使用 no-store。
        html = (web_dir / filename).read_text(encoding="utf-8")
        html = html.replace('<body>', f'<body data-workspace-view="{view}">')
        html = html.replace('<body ', f'<body data-team-mode="{str(access.team_mode).lower()}" ', 1)
        for asset in ("app.js", "style.css", "workspace.js", "workspace.css", "admin-workspace.js", "knowledge.js", "product.css", "login.js", "conversation.js", "conversation.css", "admin.css", "admin.js", "setup.js", "unified-chat.js", "unified-chat.css"):
            html = html.replace(f'"/static/{asset}"', f'"/static/{asset}?v={runtime.fingerprint}"')
        return HTMLResponse(html)

    @app.get("/", include_in_schema=False)
    @app.get("/app", include_in_schema=False)
    def home():
        return workspace_page('unified-chat.html', 'conversation')

    @app.get('/single', include_in_schema=False)
    def independent_questions(request:Request):
        from fastapi.responses import RedirectResponse
        from urllib.parse import urlencode
        job=request.query_params.get('job')
        return RedirectResponse('/app'+('?' + urlencode({'chat':'q:'+job}) if job else ''),status_code=303)

    @app.get("/admin", include_in_schema=False)
    def admin_workspace():
        return workspace_page("admin.html", "knowledge")

    @app.get('/admin/users', include_in_schema=False)
    def admin_users():
        return workspace_page('admin.html', 'users')

    @app.get('/admin/feedback', include_in_schema=False)
    def admin_feedback():
        return workspace_page('feedback-admin.html')

    @app.get('/admin/settings', include_in_schema=False)
    def admin_settings():
        return workspace_page('admin.html', 'settings')

    @app.get('/admin/documents', include_in_schema=False)
    def admin_documents():
        return workspace_page('index.html', 'admin')

    @app.get("/tools", include_in_schema=False)
    def document_tools():
        return workspace_page("index.html", "tools")

    @app.get('/knowledge', include_in_schema=False)
    def knowledge_workspace():
        return workspace_page('knowledge.html', 'knowledge')

    @app.get('/login', include_in_schema=False)
    def login_page():
        return workspace_page('login.html', 'login')

    @app.get('/setup', include_in_schema=False)
    def setup_page():
        return workspace_page('setup.html', 'setup')

    @app.get('/account/password',include_in_schema=False)
    def account_password_page():
        return workspace_page('account-password.html','account-password')

    install_product_routes(app, access)
    install_profile_routes(app, access)
    install_admin_routes(app, access)
    install_conversation_routes(app, conversations)
    from app.knowledge_routes import install_knowledge_routes
    install_knowledge_routes(app, access, rag)
    from app.unified_routes import install_unified_routes
    install_unified_routes(app,access,rag,conversations)

    @app.get("/api/health")
    def health():
        # 报告真正运行的代码、数据目录及结构版本；不探测外部模型账号。
        return {"status": "ok", "version": "0.2.0", "runtime": runtime.check()}

    @app.get("/api/runtime/status")
    def runtime_status():
        # 只展示经过进程创建标识核实的就绪状态，陈旧 ready 文件不能冒充活跃 worker。
        return JSONResponse(runtime.service_status(), headers={"Cache-Control": "no-store"})

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
            "max_upload_mb": settings.max_upload_mb,
            "formats": {"md": True, "txt": True, "pdf": True, "docx": True, "xlsx": True,
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
            "embedding": True, "retrieval": True, "rag": True, "summary": True,
            "extraction": True, "web_crawl": False, "browser_extension": False,
            "model_adapters": {"deepseek": True, "qwen": False, "chatglm": False, "llama": False},
            "model_configured": bool(settings.deepseek_api_key.strip()),
            "embedding_configured": bool(settings.embedding_api_key.strip()),
            # 当前产品能力与旧同步接口分别说明，不能用旧契约描述全部会话。
            "rag_mode": "document-and-knowledge-conversations",
            "rag_prompt_version": PROMPT_VERSION,
            "rag_prompt_scope": "legacy_sync_questions",
            "legacy_sync": {"rag_mode": "single-document-single-turn-non-streaming",
                            "endpoint": "/api/documents/{id}/questions", "persistent": False},
            "conversations": {"document": True, "knowledge_base": True, "persistent": True,
                              "memory_control": True, "structured_object_memory": False},
            "knowledge_bases": {"publish_snapshots": True, "member_authorization": True,
                                "multi_document_retrieval": True},
            "task_question": {
                "prompt_version": TASK_PROMPT_VERSION,
                "answer_contract": ANSWER_CONTRACT_VERSION,
            },
            "streaming": {"enabled": True, "protocol": STREAM_VERSION,
                          "delivery": "validated-fact-sse", "persistent_events": True,
                          "reconnect_generates": False, "legacy_sync": False},
            "rag_configured": rag.ready()[0],
            # 分析能力：摘要／提取基于解析版本，不依赖 embedding 索引；
            # 任务由独立 worker 执行，能力查询本身不产生任何收费调用。
            "analysis": {
                "enabled": True,
                "requires_embedding_index": False,
                "requires_worker": True,
                "configured": analysis.ready()[0],
                "extraction_prompt_version": EXTRACTION_PROMPT_VERSION,
                "summary_prompt_version": SUMMARY_PROMPT_VERSION,
                "max_requests_per_job": settings.analysis_max_requests,
            },
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
        return access.visible_documents(repository.list_documents())

    @app.post("/api/documents", response_model=UploadResponse, status_code=201)
    def upload_document(file: UploadFile = File(...), idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
        """上传文档：内容识别格式 → 大小限制 → 服务端随机 ID 落盘 → 写入元数据。

        上传不会自动建立收费索引；解析也必须由用户显式触发。
        """
        from app.uploads import store_upload
        return store_upload(repository, settings, file, idempotency_key,
                            scope=access.actor['id'] if access.actor else 'local')

    @app.get("/api/documents/{document_id}", response_model=Document)
    def get_document(document_id: str):
        # 查询单份文档：包含当前预览版本、可用索引版本、最近任务与质量状态。
        return access.present_document(require_document(document_id))

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
        versions = repository.list_parse_versions(document_id)
        if access.team_mode and access.actor['role'] != 'admin':
            published = access.require_document(document_id)
            if published:
                versions = [v for v in versions if v.id == published['parse_version_id']]
        return versions

    @app.get("/api/documents/{document_id}/content")
    def document_content(document_id: str, version_id: str | None = None,
                         offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        """受控返回预览内容与来源；较大结果分页读取，返回稳定契约。"""
        document = require_document(document_id)
        published = access.require_document(document_id)
        target = version_id or (published['parse_version_id'] if published else document.active_parse_version_id)
        if not target:
            raise HTTPException(404, "该文档尚无成功解析版本")
        version = require_version(document_id, target)
        blocks = repository.blocks(target, limit=limit, offset=offset)
        # legacy 版本只有旧分块，不能伪造结构块或可靠来源；只读兼容展示原有文本。
        legacy_chunks = repository.chunks(document_id, target) if version.is_legacy else []
        visible_legacy = legacy_chunks[offset:offset + limit]
        return {
            "document_id": document_id,
            "version": version,
            "is_active_version": target == document.active_parse_version_id,
            "offset": offset, "limit": limit,
            "content_mode": "legacy_chunks" if version.is_legacy else "structured",
            "returned": len(visible_legacy) if version.is_legacy else len(blocks),
            "total": len(legacy_chunks) if version.is_legacy else version.block_count,
            "legacy_chunks": visible_legacy,
            "blocks": blocks,
        }

    @app.get("/api/documents/{document_id}/chunks", response_model=list[Chunk])
    def chunks(document_id: str, version_id: str | None = None):
        """默认返回活动解析版本的分块；可读取属于该文档的历史版本分块。

        不允许串文档读取：version_id 必须属于该文档。
        """
        document = require_document(document_id)
        published = access.require_document(document_id)
        target = version_id or (published['parse_version_id'] if published else document.active_parse_version_id)
        if target:
            require_version(document_id, target)
        return repository.chunks(document_id, target)

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
        if key and len(key) > 200:
            raise HTTPException(422, "幂等键不能超过200个字符")
        fingerprint = _request_fingerprint(request)

        if key:
            existing = repository.find_task_by_idempotency(document_id, key)
            if existing is not None:
                stored = repository.parse_request_fingerprint(existing, key)
                if stored and stored != fingerprint:
                    raise HTTPException(409, "该幂等键已用于不同的解析请求，请更换幂等键或改用原请求参数")
                return JSONResponse(status_code=202, content=ParseSubmitResponse(
                    task=existing, document=require_document(document_id), reused=True,
                    message="已存在相同幂等键的任务，直接复用，不会重复提交上游").model_dump())

        if document.active_parse_version_id and not request.force:
            # 已有有效结果且未要求重新解析：明确复用，不调用解析服务。
            version = repository.get_parse_version(document.active_parse_version_id)
            task = repository.get_task(version.task_id) if version and version.task_id else None
            if key and task:
                task = repository.bind_parse_request_key(task.id, document_id, key, fingerprint)
            return ParseSubmitResponse(
                task=task, document=document, reused=True,
                message="该文档已有成功解析版本；如需重新解析请使用“重新解析”并传入 force=true")

        active = repository.find_active_task(document_id)
        if active is not None:
            # 活动任务重复点击：复用同一个任务，不产生第二次上游提交。
            if key:
                active = repository.bind_parse_request_key(active.id, document_id, key, fingerprint)
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
        if task.status == "succeeded":
            raise HTTPException(409, "该任务已成功；如需重新解析请使用重新解析入口并传入 force=true")
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
        published = access.require_document(document_id)
        info = access.published_index_info(published) if published else index.index_info(document_id)
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
    # 分析任务（摘要 / 信息提取）
    #
    # 契约变化说明：原 `/summary` 与 `/extract` 是未接入占位（503）。
    # 本阶段改为任务式响应：202 表示已创建或复用进行中的任务，200 表示复用已有成功
    # 结果（不新增任何计费调用）。规划、状态查询、结果读取与导出都**不会**触发生成。
    # ------------------------------------------------------------------
    def analysis_ready() -> None:
        """缺生成模型配置时返回 503；不发起任何在线调用。"""
        ok, reason = analysis.ready()
        if not ok:
            raise HTTPException(503, reason)

    @app.post("/api/documents/{document_id}/analysis-plan", response_model=AnalysisPlan)
    def analysis_plan(document_id: str, kind: str = Query("extraction", pattern="^(extraction|summary)$"),
                      version_id: str | None = None,
                      payload: AnalysisPlanRequest | None = Body(default=None)):
        """只做本地输入规划：返回版本、范围、批次、请求数上界与是否可执行。

        该接口零外部调用：不生成、不 embedding、不解析、不建索引。
        """
        analysis_ready()
        outcome = analysis.build_plan(document_id, kind, version_id,
                                      payload.extraction_options if payload else None)
        return analysis.plan_response(outcome, kind)

    def submit_analysis(document_id: str, kind: str, payload: AnalysisJobRequest | None,
                        idempotency_key: str | None):
        """摘要与提取共用的提交逻辑：只写数据库，不发起生成调用。

        前置检查顺序由服务层保证（文档存在 → 可用解析版本 → 模型已配置），
        因此这里不提前做能力检查，避免“缺配置”掩盖更准确的缺失原因。
        """
        request = payload or AnalysisJobRequest()
        if idempotency_key and not request.idempotency_key:
            # model_copy不重新验证字段；Header必须遵守与请求体相同的长度限制。
            if len(idempotency_key) > 200:
                raise HTTPException(422, "幂等键不能超过200个字符")
            request = request.model_copy(update={"idempotency_key": idempotency_key})
        response, status = analysis.submit(document_id, kind, request)
        return JSONResponse(status_code=status, content=response.model_dump())

    @app.post("/api/documents/{document_id}/extract")
    def extract(document_id: str, payload: AnalysisJobRequest | None = Body(default=None),
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        """创建或复用信息提取任务（数据／结论／观点）。"""
        return submit_analysis(document_id, "extraction", payload, idempotency_key)

    @app.post("/api/documents/{document_id}/summary")
    def summarize(document_id: str, payload: AnalysisJobRequest | None = Body(default=None),
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        """创建或复用文档摘要任务。"""
        return submit_analysis(document_id, "summary", payload, idempotency_key)

    @app.get("/api/analysis-jobs/{job_id}", response_model=AnalysisJob)
    def get_analysis_job(job_id: str):
        """任务状态、阶段、进度、预算与结果入口；不返回模型原始正文。"""
        return analysis.get_job(job_id)

    @app.get("/api/analysis-jobs/{job_id}/calls")
    def analysis_job_calls(job_id: str):
        """调用账本：每次外部请求的顺序、角色、状态与耗时（失败与不确定调用都计入）。"""
        job = analysis.get_job(job_id)
        calls = repository.analysis_calls(job_id)
        return {"job_id": job_id, "requests_used": job.requests_used,
                "max_requests": job.max_requests, "calls": [call.model_dump() for call in calls]}

    @app.post("/api/analysis-jobs/{job_id}/retry", response_model=AnalysisJob)
    def retry_analysis_job(job_id: str):
        """明确重试失败或待处理任务：预算不重置，已校验步骤直接复用。"""
        analysis_ready()
        return analysis.retry_job(job_id)

    @app.post("/api/analysis-jobs/{job_id}/cancel")
    def cancel_analysis_job(job_id: str):
        """取消后续处理；重复取消幂等；不承诺取消已在途的计费。"""
        return analysis.cancel_job(job_id)

    @app.get("/api/documents/{document_id}/analysis-jobs", response_model=list[AnalysisJob])
    def list_analysis_jobs(document_id: str):
        """该文档的分析任务历史；只读，不触发生成。"""
        require_document(document_id)
        return repository.list_analysis_jobs(document_id)

    @app.get("/api/documents/{document_id}/analysis-results",
             response_model=list[AnalysisResultSummary])
    def list_analysis_results(document_id: str,
                              kind: str | None = Query(None, pattern="^(extraction|summary)$")):
        """该文档已校验结果历史，包含版本与是否为当前预览版本。"""
        return analysis.list_results(document_id, kind)

    @app.get("/api/analysis-results/{result_id}", response_model=AnalysisResult)
    def get_analysis_result(result_id: str):
        """返回持久化结果；只按结果 ID 读取，不接受任意路径或版本参数。"""
        return analysis.get_result(result_id)

    @app.get("/api/analysis-results/{result_id}/export")
    def export_analysis_result(result_id: str,
                               format: str = Query("markdown", pattern="^(markdown|json)$")):
        """导出已校验结果及必要来源信息。

        不导出凭证、模型思考或原始模型响应；文件名做安全化处理。
        """
        fmt = "json" if format == "json" else "markdown"
        body, media_type, filename = analysis.export(result_id, fmt)
        return Response(
            content=body, media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"})

    @app.get("/api/analysis/status")
    def analysis_status():
        """分析能力状态：只报告配置与预算，不调用任何收费接口，也不探测账号可用性。"""
        ok, reason = analysis.ready()
        return {
            "extraction_prompt_version": EXTRACTION_PROMPT_VERSION,
            "summary_prompt_version": SUMMARY_PROMPT_VERSION,
            "configured": ok,
            "note": reason,
            "model": settings.deepseek_model,
            "max_requests_per_job": settings.analysis_max_requests,
            "batch_max_chars": settings.analysis_batch_max_chars,
            "reduce_max_chars": settings.analysis_reduce_max_chars,
            "input_max_chars": settings.analysis_input_max_chars,
            "max_document_chars": settings.analysis_max_document_chars,
            "max_items_per_batch": settings.analysis_max_items_per_batch,
            "max_items_total": settings.analysis_max_items_total,
            "budget_unit": "characters",
            # 需要独立 worker 进程真正运行，任务才会被执行。
            "requires_worker": True,
            "worker_command": "python -m app.analysis_worker",
            # 分析基于解析版本，不依赖 embedding 索引。
            "requires_embedding_index": False,
        }

    @app.get("/api/rag/status")
    def rag_status():
        """问答能力状态：只报告配置与预算，不调用任何收费接口。"""
        return {
            "prompt_version": PROMPT_VERSION,
            "mode": "single-document-single-turn-non-streaming",
            "configured": rag.ready()[0],
            "note": rag.ready()[1],
            "retrieval_k": settings.rag_retrieval_k,
            "context_k": settings.rag_context_k,
            "context_max_chars": settings.rag_context_max_chars,
            "input_max_chars": settings.rag_input_max_chars,
            "task_question": {
                "context_k": settings.question_context_k,
                "candidate_k": settings.question_candidate_k,
                "context_max_chars": settings.question_context_max_chars,
                "input_max_chars": settings.question_input_max_chars,
            },
            "min_score": settings.rag_min_score,
            # 字符预算不是 token 预算；真正的模型上下文限制仍需供应商实测。
            "budget_unit": "characters",
            # 本阶段没有持久化幂等保证：两个独立合法 POST 会产生两次费用。
            "idempotent": False,
            "persists_history": False,
            "question_jobs": {"available": True, "idempotent": True, "persists_history": True,
                              "max_requests": settings.question_max_requests,
                              "requires_worker": "analysis-worker"},
        }

    @app.post("/api/documents/{document_id}/question-jobs")
    def submit_question_job(document_id: str, payload: QuestionJobRequest, request: Request):
        # 新入口只固定本地快照并入队；保留下面原同步 /questions 契约。
        published = access.require_document(document_id)
        snapshot_id = None
        if published:
            snapshot_id = published['snapshot_id']
        body, code = questions.submit(document_id, payload, request_id=request.state.request_id,
            owner_user_id=access.actor['id'] if access.actor else None, knowledge_snapshot_id=snapshot_id)
        return JSONResponse(status_code=code, content=body,
                            headers={"Location": f"/api/question-jobs/{body['job']['id']}"})

    @app.get("/api/documents/{document_id}/question-jobs")
    def question_history(document_id: str):
        access.require_document(document_id)
        jobs = questions.list_jobs(document_id)
        if access.team_mode:
            jobs = [j for j in jobs if j.get('owner_user_id') == access.actor['id'] or
                    (j.get('owner_user_id') is None and access.actor['role'] == 'admin')]
        return jobs

    @app.get("/api/question-jobs/{job_id}")
    def question_job(job_id: str):
        access.require_question(job_id)
        return questions.get_job(job_id)

    @app.post("/api/question-jobs/{job_id}/cancel")
    def cancel_question_job(job_id: str, payload: QuestionRetryRequest | None = Body(default=None)):
        access.require_question(job_id)
        return questions.cancel_job(job_id, payload)

    @app.post("/api/question-jobs/{job_id}/retry")
    def retry_question_job(job_id: str, payload: QuestionRetryRequest):
        access.require_question(job_id)
        return questions.retry_job(job_id, payload)

    @app.get("/api/question-results/{result_id}")
    def question_result(result_id: str):
        result = questions.get_result(result_id)
        access.require_question(result['job_id'])
        return result

    @app.get("/api/question-results/{result_id}/export")
    def question_export(result_id: str, fmt: str = Query(default="markdown", alias="format")):
        access.require_question(questions.get_result(result_id)['job_id'])
        body, media_type, filename = questions.export(result_id, fmt)
        return Response(content=body, media_type=media_type,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"})

    @app.post("/api/documents/{document_id}/questions", response_model=RagAnswer,
              response_model_exclude={"answer_parts": True,
                                      "conclusion": {"__all__": {"context", "claim_type", "evidence_ids", "support_review"}},
                                      "explanation": {"__all__": {"context", "claim_type", "evidence_ids", "support_review"}},
                                      "citations": {"__all__": {"table_structure", "source_reference_id", "evidence_id", "quote_sha256",
                                          "char_start", "char_end", "offset_basis", "verification", "semantic_status"}}})
    def ask(document_id: str, payload: Question):
        """问答接口：单文档、单轮、非流式，回答带逐事实引用。

        前置检查顺序：请求结构（FastAPI + Question 模型）→ 文档存在 →
        成功解析版本 → 与当前 embedding 配置兼容的可用索引 → 两个在线模型已配置 →
        检索 → 生成。任何前置条件失败都不会调用生成，也不会调用查询 embedding。
        """
        return rag.answer(document_id, payload.question.strip()).answer

    from app.attachments import install_attachment_routes
    install_attachment_routes(app, conversations, parse, build_index)
    from app.answer_stream_routes import install_answer_stream_routes
    install_answer_stream_routes(app, access, questions)
    from app.answer_feedback import install_feedback_routes
    install_feedback_routes(app,access,questions)
    from app.resource_governance import install_governance_routes
    install_governance_routes(app,access)
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
