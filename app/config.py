# 集中读取运行配置；此模块不自动加载 .env 文件。
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # 创建配置实例时读取环境变量；冻结实例，避免运行中被意外修改。
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DOCQA_DATA_DIR", "data")).resolve())
    # 单位为 MB，上传接口校验时转换为字节数。
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("DOCQA_MAX_UPLOAD_MB", "20")))

    def __post_init__(self):
        # 启动前拒绝无效限制，避免所有上传请求都因配置问题失败。
        if self.max_upload_mb <= 0:
            raise ValueError("DOCQA_MAX_UPLOAD_MB 必须大于 0")
