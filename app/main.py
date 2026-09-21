# 应用入口：装配依赖、注册 API，并提供同源静态工作台。
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.parsing import ParseError, chunk_pages, parse_document
from app.repository import Repository
from app.schemas import Answer, Chunk, Document, Extraction, Question, Summary

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    # 应用工厂支持注入配置，测试可使用独立临时目录而不污染实际资料。
    settings = settings or Settings()
    repository = Repository(settings.data_dir / "docqa.db")
    upload_dir = settings.data_dir / "uploads"
    web_dir = Path(__file__).parent / "web"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 在服务启动阶段准备目录和数据库；yield 后进入服务关闭阶段。
        upload_dir.mkdir(parents=True, exist_ok=True)
        repository.initialize()
        yield

    app = FastAPI(title="智能文档分析与知识问答系统", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    def require_document(document_id: str) -> Document:
        # 各文档接口共用存在性校验，统一返回 404。
        document = repository.get(document_id)
        if document is None:
            raise HTTPException(404, "文档不存在")
        return document

    def require_intelligence(document_id: str):
        # 先检查解析前置条件，再明确拒绝尚未接入的智能能力，避免伪造结果。
        document = require_document(document_id)
        if document.status != "parsed":
            raise HTTPException(409, "请先成功解析文档")
        raise HTTPException(503, "智能能力尚未接入：需配置大模型、Embedding 和向量检索适配器")

    @app.get("/", include_in_schema=False)
    def home():
        # HTML 和静态资源与 API 同源，前端使用相对路径调用后端。
        return FileResponse(web_dir / "index.html")

    @app.get("/api/health")
    def health():
        # 仅报告应用可响应，不代表外部模型或检索服务可用。
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/capabilities")
    def capabilities():
        # 能力清单反映当前实现状态；协议接口存在不等于能力已经接入。
        return {"upload": True, "text_pdf_parsing": True, "utf8_txt_parsing": True,
                "ocr": False, "embedding": False, "rag": False, "summary": False,
                "extraction": False, "model_adapters": {"qwen": False, "chatglm": False, "llama": False}}

    @app.get("/api/documents", response_model=list[Document])
    def list_documents():
        # 返回工作台所需的元数据列表，正文通过分块接口单独获取。
        return repository.list_documents()

    @app.post("/api/documents", response_model=Document, status_code=201)
    def upload_document(file: UploadFile = File(...)):
        # 去掉客户端可能携带的路径，仅将原文件名用于显示和格式判断。
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".txt"}:
            file.file.close()
            raise HTTPException(415, "框架阶段支持 PDF 和 UTF-8 TXT 文件")
        # 用服务端随机 ID 存储原件，避免用户文件名决定落盘路径。
        document_id = uuid4().hex
        target = upload_dir / document_id
        size = 0
        try:
            with target.open("xb") as output:
                # 分批从上传临时文件读取，避免将整份资料一次性加载到内存。
                while block := file.file.read(1024 * 1024):
                    size += len(block)
                    if size > settings.max_upload_mb * 1024 * 1024:
                        raise HTTPException(413, f"文件不得超过 {settings.max_upload_mb} MB")
                    output.write(block)
            if size == 0:
                raise HTTPException(422, "文件为空")
            document = Document(id=document_id, filename=filename, size=size,
                                created_at=datetime.now(timezone.utc).isoformat(), status="uploaded")
            repository.create(document)
            return document
        except Exception:
            # 上传校验或元数据写入失败时，清理本次原件，避免残留无记录文件。
            target.unlink(missing_ok=True)
            raise
        finally:
            # 无论上传成功与否，都释放上传文件句柄。
            file.file.close()

    @app.get("/api/documents/{document_id}", response_model=Document)
    def get_document(document_id: str):
        # 查询单份文档，包括当前状态和上次解析错误。
        return require_document(document_id)

    @app.post("/api/documents/{document_id}/parse", response_model=Document)
    def parse(document_id: str):
        # 当前同步执行解析，接口会等待解析与分块入库完成。
        document = require_document(document_id)
        if document.status == "parsed":
            # 重复提交返回已有结果，不重新生成块 ID。
            return document
        if not repository.claim_parse(document_id):
            raise HTTPException(409, "文档正在解析，请稍后刷新")
        try:
            pages = parse_document(upload_dir / document_id, Path(document.filename).suffix.lower())
            repository.finish_parse(document_id, len(pages), chunk_pages(document_id, pages))
        except ParseError as exc:
            # 内容问题返回 422，并记录失败状态供页面展示和用户重试。
            repository.fail_parse(document_id, str(exc))
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            # 非预期异常记录完整堆栈；对外只返回通用信息，不暴露内部细节。
            logger.exception("Document parsing failed: %s", document_id)
            repository.fail_parse(document_id, "解析失败，请检查服务日志后重试")
            raise HTTPException(500, "解析失败，请检查服务日志后重试") from exc
        return require_document(document_id)

    @app.get("/api/documents/{document_id}/chunks", response_model=list[Chunk])
    def chunks(document_id: str):
        # 先校验文档，区分“文档不存在”和“尚未生成分块”。
        require_document(document_id)
        return repository.chunks(document_id)

    @app.post("/api/documents/{document_id}/summary", response_model=Summary)
    def summarize(document_id: str):
        # 预留摘要响应结构，后续在此接入分析服务。
        require_intelligence(document_id)

    @app.post("/api/documents/{document_id}/extract", response_model=Extraction)
    def extract(document_id: str):
        # 预留数据、结论、观点提取入口。
        require_intelligence(document_id)

    @app.post("/api/documents/{document_id}/questions", response_model=Answer)
    def ask(document_id: str, payload: Question):
        # FastAPI 自动校验问题；后续由 RAG 服务检索上下文并生成带引用回答。
        require_intelligence(document_id)

    return app


# uvicorn app.main:app 使用的默认应用实例。
app = create_app()
