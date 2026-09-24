# 集中读取运行配置；应用通过 from_env 加载本地 .env，环境变量优先。
import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    # 创建配置实例时读取环境变量；冻结实例，避免运行中被意外修改。
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DOCQA_DATA_DIR", "data")).resolve())
    # 单位为 MB，上传接口校验时转换为字节数。
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("DOCQA_MAX_UPLOAD_MB", "20")))
    # Docling 解析服务地址只来自后端配置，前端不能覆盖；默认本机独立服务。
    docling_base_url: str = field(default_factory=lambda: os.getenv("DOCQA_DOCLING_BASE_URL", "http://127.0.0.1:5001"))
    # 连接超时、单次读取超时、整体等待期限分别配置，避免无限阻塞。
    docling_connect_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("DOCQA_DOCLING_CONNECT_TIMEOUT_SECONDS", "10")))
    docling_read_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("DOCQA_DOCLING_READ_TIMEOUT_SECONDS", "120")))
    docling_total_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("DOCQA_DOCLING_TOTAL_TIMEOUT_SECONDS", "1800")))
    # 轮询间隔与有限重试（查询、领取结果可重试；POST 不自动重试）。
    docling_poll_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("DOCQA_DOCLING_POLL_INTERVAL_SECONDS", "2")))
    docling_max_retries: int = field(default_factory=lambda: int(os.getenv("DOCQA_DOCLING_MAX_RETRIES", "3")))
    docling_retry_backoff_seconds: float = field(
        default_factory=lambda: float(os.getenv("DOCQA_DOCLING_RETRY_BACKOFF_SECONDS", "2")))
    # 解析参数：固定 RapidOCR + 中文 + accurate 表格，不默认启用 VLM、图片描述或公式增强。
    docling_ocr_engine: str = field(default_factory=lambda: os.getenv("DOCQA_DOCLING_OCR_ENGINE", "rapidocr"))
    docling_ocr_lang: str = field(default_factory=lambda: os.getenv("DOCQA_DOCLING_OCR_LANG", "ch"))
    docling_table_mode: str = field(default_factory=lambda: os.getenv("DOCQA_DOCLING_TABLE_MODE", "accurate"))
    docling_image_export_mode: str = field(
        default_factory=lambda: os.getenv("DOCQA_DOCLING_IMAGE_EXPORT_MODE", "placeholder"))
    # 允许 PDF 的最大页数：不得超过已配置解析服务能力，超限返回明确失败。
    max_pdf_pages: int = field(default_factory=lambda: int(os.getenv("DOCQA_MAX_PDF_PAGES", "100")))
    # worker 串行处理，租约与轮询间隔决定崩溃后可恢复的时间窗口。
    worker_lease_seconds: int = field(default_factory=lambda: int(os.getenv("DOCQA_WORKER_LEASE_SECONDS", "300")))
    worker_idle_sleep_seconds: float = field(
        default_factory=lambda: float(os.getenv("DOCQA_WORKER_IDLE_SLEEP_SECONDS", "3")))
    # 分块：长度单位为字符（不是 token），上限与 embedding 请求限制协调。
    chunk_max_chars: int = field(default_factory=lambda: int(os.getenv("DOCQA_CHUNK_MAX_CHARS", "800")))
    chunk_overlap_chars: int = field(default_factory=lambda: int(os.getenv("DOCQA_CHUNK_OVERLAP_CHARS", "120")))
    # 表格按行组分块的行数上限，保证长表分块携带表头与口径。
    table_rows_per_chunk: int = field(default_factory=lambda: int(os.getenv("DOCQA_TABLE_ROWS_PER_CHUNK", "20")))
    # 密钥不进入配置对象的 repr，避免调试输出意外包含凭证。
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_thinking: str = "enabled"
    deepseek_reasoning_effort: str = "high"
    deepseek_timeout_seconds: float = 120
    deepseek_max_tokens: int = 8192
    # Embedding 网关使用独立密钥，绝不自动复用 DeepSeek 的密钥。
    embedding_api_key: str = field(default="", repr=False)
    embedding_base_url: str = "https://tokendance.space/gateway/v1"
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_timeout_seconds: float = 60
    embedding_batch_size: int = 8

    @classmethod
    def from_env(cls, env_file: Path = Path(".env")):
        # 只读取指定文件，不修改进程环境；系统环境变量覆盖文件中的同名配置。
        values = {**dotenv_values(env_file, encoding="utf-8-sig"), **os.environ}
        def value(name: str, default: str) -> str:
            return (values.get(name) or default).strip()
        return cls(
            data_dir=Path(value("DOCQA_DATA_DIR", "data")).resolve(),
            max_upload_mb=int(value("DOCQA_MAX_UPLOAD_MB", "20")),
            docling_base_url=value("DOCQA_DOCLING_BASE_URL", "http://127.0.0.1:5001"),
            docling_connect_timeout_seconds=float(value("DOCQA_DOCLING_CONNECT_TIMEOUT_SECONDS", "10")),
            docling_read_timeout_seconds=float(value("DOCQA_DOCLING_READ_TIMEOUT_SECONDS", "120")),
            docling_total_timeout_seconds=float(value("DOCQA_DOCLING_TOTAL_TIMEOUT_SECONDS", "1800")),
            docling_poll_interval_seconds=float(value("DOCQA_DOCLING_POLL_INTERVAL_SECONDS", "2")),
            docling_max_retries=int(value("DOCQA_DOCLING_MAX_RETRIES", "3")),
            docling_retry_backoff_seconds=float(value("DOCQA_DOCLING_RETRY_BACKOFF_SECONDS", "2")),
            docling_ocr_engine=value("DOCQA_DOCLING_OCR_ENGINE", "rapidocr"),
            docling_ocr_lang=value("DOCQA_DOCLING_OCR_LANG", "ch"),
            docling_table_mode=value("DOCQA_DOCLING_TABLE_MODE", "accurate"),
            docling_image_export_mode=value("DOCQA_DOCLING_IMAGE_EXPORT_MODE", "placeholder"),
            max_pdf_pages=int(value("DOCQA_MAX_PDF_PAGES", "100")),
            worker_lease_seconds=int(value("DOCQA_WORKER_LEASE_SECONDS", "300")),
            worker_idle_sleep_seconds=float(value("DOCQA_WORKER_IDLE_SLEEP_SECONDS", "3")),
            chunk_max_chars=int(value("DOCQA_CHUNK_MAX_CHARS", "800")),
            chunk_overlap_chars=int(value("DOCQA_CHUNK_OVERLAP_CHARS", "120")),
            table_rows_per_chunk=int(value("DOCQA_TABLE_ROWS_PER_CHUNK", "20")),
            deepseek_api_key=value("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=value("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=value("DEEPSEEK_MODEL", "deepseek-flash"),
            deepseek_thinking=value("DEEPSEEK_THINKING", "enabled"),
            deepseek_reasoning_effort=value("DEEPSEEK_REASONING_EFFORT", "high"),
            deepseek_timeout_seconds=float(value("DEEPSEEK_TIMEOUT_SECONDS", "120")),
            deepseek_max_tokens=int(value("DEEPSEEK_MAX_TOKENS", "8192")),
            embedding_api_key=value("EMBEDDING_API_KEY", ""),
            embedding_base_url=value("EMBEDDING_BASE_URL", "https://tokendance.space/gateway/v1"),
            embedding_model=value("EMBEDDING_MODEL", "qwen3.7-text-embedding"),
            embedding_timeout_seconds=float(value("EMBEDDING_TIMEOUT_SECONDS", "60")),
            embedding_batch_size=int(value("EMBEDDING_BATCH_SIZE", "8")),
        )

    def __post_init__(self):
        # 启动前拒绝无效限制，避免所有上传请求都因配置问题失败。
        if self.max_upload_mb <= 0:
            raise ValueError("DOCQA_MAX_UPLOAD_MB 必须大于 0")
        # 基础地址只能来自服务端配置，不允许前端请求改变密钥发送目的地。
        url = urlsplit(self.deepseek_base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("DEEPSEEK_BASE_URL 必须是无凭证、查询参数和片段的 HTTPS API 基础地址")
        if not self.deepseek_model.strip():
            raise ValueError("DEEPSEEK_MODEL 不能为空")
        if self.deepseek_thinking not in {"enabled", "disabled"}:
            raise ValueError("DEEPSEEK_THINKING 只支持 enabled 或 disabled")
        if self.deepseek_reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("DEEPSEEK_REASONING_EFFORT 只支持 low、high、max")
        if not math.isfinite(self.deepseek_timeout_seconds) or self.deepseek_timeout_seconds <= 0:
            raise ValueError("DEEPSEEK_TIMEOUT_SECONDS 必须是有限正数")
        if self.deepseek_max_tokens <= 0:
            raise ValueError("DEEPSEEK_MAX_TOKENS 必须大于 0")
        url = urlsplit(self.embedding_base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("EMBEDDING_BASE_URL 必须是无凭证、查询参数和片段的 HTTPS API 基础地址")
        if not self.embedding_model.strip():
            raise ValueError("EMBEDDING_MODEL 不能为空")
        if not math.isfinite(self.embedding_timeout_seconds) or self.embedding_timeout_seconds <= 0:
            raise ValueError("EMBEDDING_TIMEOUT_SECONDS 必须是有限正数")
        if not 1 <= self.embedding_batch_size <= 32:
            raise ValueError("EMBEDDING_BATCH_SIZE 必须在 1 到 32 之间")
        # Docling 地址同样只来自后端配置：允许本机 http（独立解析服务），
        # 但禁止内嵌凭据、查询参数与片段，且必须是 http/https 绝对地址。
        docling = urlsplit(self.docling_base_url)
        if docling.scheme not in {"http", "https"} or not docling.hostname:
            raise ValueError("DOCQA_DOCLING_BASE_URL 必须是 http 或 https 的绝对地址")
        if docling.username or docling.password or docling.query or docling.fragment:
            raise ValueError("DOCQA_DOCLING_BASE_URL 不能包含凭据、查询参数或片段")
        if docling.path not in {"", "/"}:
            raise ValueError("DOCQA_DOCLING_BASE_URL 只填写服务根地址，不要包含路径")
        for name, seconds in (("DOCQA_DOCLING_CONNECT_TIMEOUT_SECONDS", self.docling_connect_timeout_seconds),
                              ("DOCQA_DOCLING_READ_TIMEOUT_SECONDS", self.docling_read_timeout_seconds),
                              ("DOCQA_DOCLING_TOTAL_TIMEOUT_SECONDS", self.docling_total_timeout_seconds),
                              ("DOCQA_DOCLING_POLL_INTERVAL_SECONDS", self.docling_poll_interval_seconds),
                              ("DOCQA_DOCLING_RETRY_BACKOFF_SECONDS", self.docling_retry_backoff_seconds),
                              ("DOCQA_WORKER_IDLE_SLEEP_SECONDS", self.worker_idle_sleep_seconds)):
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f"{name} 必须是有限正数")
        if self.docling_read_timeout_seconds > self.docling_total_timeout_seconds:
            raise ValueError("DOCQA_DOCLING_READ_TIMEOUT_SECONDS 不能大于整体等待期限")
        if not 0 <= self.docling_max_retries <= 10:
            raise ValueError("DOCQA_DOCLING_MAX_RETRIES 必须在 0 到 10 之间")
        if not self.docling_ocr_engine.strip() or not self.docling_ocr_lang.strip():
            raise ValueError("DOCQA_DOCLING_OCR_ENGINE 与 DOCQA_DOCLING_OCR_LANG 不能为空")
        if self.max_pdf_pages <= 0:
            raise ValueError("DOCQA_MAX_PDF_PAGES 必须大于 0")
        if self.worker_lease_seconds < 30:
            raise ValueError("DOCQA_WORKER_LEASE_SECONDS 不能小于 30 秒")
        if self.chunk_max_chars <= 0 or not 0 <= self.chunk_overlap_chars < self.chunk_max_chars:
            raise ValueError("分块长度必须大于 0，且重叠长度必须小于分块长度")
        if self.table_rows_per_chunk <= 0:
            raise ValueError("DOCQA_TABLE_ROWS_PER_CHUNK 必须大于 0")
