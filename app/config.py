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
