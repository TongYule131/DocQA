# Pydantic 数据契约：用于请求校验、响应序列化和接口文档生成。
#
# 本阶段在原有契约上补充三组彼此独立的状态（对应任务书 3.1）：
#   1. 任务状态 ParseTask.status（本次解析是否排队/执行/失败/完成）；
#   2. 内容质量 ParseVersion.quality_status + QualityWarning（结构是否可用、有何告警）；
#   3. 索引状态 IndexInfo.status（哪个解析版本的向量可用）。
# 任何一组状态都不再用 documents.status 一个枚举表达。
from typing import Any, Literal

from pydantic import BaseModel, Field

# 支持的输入格式：TXT 在本地解析，其余交给 Docling。
DocumentFormat = Literal["txt", "pdf", "docx", "xlsx"]

# 任务状态机：queued → running → succeeded / failed；提交结果不确定时进入 needs_attention。
ParseTaskStatus = Literal["queued", "running", "succeeded", "failed", "needs_attention"]

# 执行阶段用于页面展示“正在做什么”，不代表完成百分比。
ParseStage = Literal[
    "queued", "submitting", "waiting_upstream", "fetching_result",
    "normalizing", "chunking", "publishing", "done",
]

# 内容质量：ok 无告警；warnings 可用但有限制；invalid 结构无效，不发布。
QualityStatus = Literal["ok", "warnings", "invalid"]
WarningSeverity = Literal["info", "warning", "error"]

# 索引状态：pending 未建立；indexing 构建中；indexed 可用；failed 失败；stale 配置或来源已变。
IndexStatus = Literal["pending", "indexing", "indexed", "failed", "stale", "superseded"]


class Page(BaseModel):
    # 兼容旧的本地解析逻辑页；PDF 为物理页码，TXT 为逻辑页。
    number: int
    text: str


class SourceLocation(BaseModel):
    """来源记录：一个块可以对应多个来源，格式与单位必须显式声明。

    不把 DOCX/XLSX 的来源强制转换成 PDF 页码；无真实页码时 page 为 None。
    """
    format: Literal["pdf", "docx", "xlsx", "txt"]
    node_ref: str | None = None
    # PDF：一基页号、原始 bbox、坐标原点与单位；空白页继续占原页号。
    page: int | None = None
    page_end: int | None = None
    bbox: dict[str, Any] | None = None
    coord_origin: str | None = None
    coord_unit: str | None = None
    # DOCX：章节路径、表编号与行列跨度。
    section_path: str | None = None
    table_no: int | None = None
    row_index: int | None = None
    col_index: int | None = None
    row_span: int | None = None
    col_span: int | None = None
    # XLSX：工作表名与真实单元格范围。
    sheet_name: str | None = None
    cell_range: str | None = None
    # TXT：行范围；其页码不是物理 PDF 页码。
    line_start: int | None = None
    line_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    note: str | None = None

    def label(self) -> str:
        """生成面向页面的中文来源标签，明确单位，不编造页码。"""
        if self.format == "pdf":
            parts = [f"第 {self.page} 页" if self.page else "页码未知"]
            if self.bbox:
                parts.append(
                    f"框 l={self.bbox.get('l')}, t={self.bbox.get('t')}, "
                    f"r={self.bbox.get('r')}, b={self.bbox.get('b')}"
                    + (f"（原点 {self.coord_origin}，单位 {self.coord_unit}）" if self.coord_origin else "")
                )
            return " · ".join(parts)
        if self.format == "docx":
            parts = []
            if self.section_path:
                parts.append(f"章节：{self.section_path}")
            if self.table_no is not None:
                parts.append(f"表格 {self.table_no} 第 {self.row_index} 行第 {self.col_index} 列")
            parts.append("DOCX 无真实页码")
            return " · ".join(parts)
        if self.format == "xlsx":
            parts = [f"工作表：{self.sheet_name}" if self.sheet_name else "工作表未知"]
            if self.cell_range:
                parts.append(f"单元格范围：{self.cell_range}")
            return " · ".join(parts)
        parts = [f"逻辑页 {self.page}" if self.page else "逻辑页未知"]
        if self.line_start is not None:
            parts.append(f"第 {self.line_start}-{self.line_end} 行")
        parts.append("TXT 页码不是物理 PDF 页码")
        return " · ".join(parts)


class QualityWarning(BaseModel):
    """质量告警：稳定告警码 + 中文说明 + 严重程度 + 受影响来源。"""
    code: str
    message: str
    severity: WarningSeverity
    scope: Literal["document", "block", "table", "page", "sheet"]
    block_id: str | None = None
    source: SourceLocation | None = None
    page: int | None = None
    sheet_name: str | None = None
    detail: str | None = None


class Block(BaseModel):
    """结构化块：按 Docling 阅读顺序排列，表格保留结构，正文保留标题路径。"""
    id: str
    document_id: str
    parse_version_id: str
    order_index: int
    # 类型：title / section_header / paragraph / list_item / table / caption / footnote /
    #      formula / page_header / page_footer / picture / unknown
    block_type: str
    label: str | None = None
    text: str = ""
    heading_path: str | None = None
    table: dict[str, Any] | None = None
    # XLSX 块所属工作表名；Markdown 不保留工作表标签，必须单独保存。
    sheet_name: str | None = None
    sources: list[SourceLocation] = Field(default_factory=list)
    char_count: int = 0


class Chunk(BaseModel):
    """检索分块：绑定唯一文档与唯一解析版本，并保留来源。

    page 为兼容字段，允许为空；DOCX/XLSX 无真实页码时不得伪造页号。
    """
    id: str
    document_id: str
    page: int | None = None
    text: str
    # 版本化字段：旧库迁移生成的 legacy 分块同样带 parse_version_id 与显式顺序。
    parse_version_id: str | None = None
    order_index: int | None = None
    block_id: str | None = None
    chunk_type: str | None = None
    heading_path: str | None = None
    sources: list[SourceLocation] = Field(default_factory=list)


class ParseVersion(BaseModel):
    """不可变解析版本：结构化结果与 Markdown 的位置、质量状态与统计。"""
    id: str
    document_id: str
    task_id: str | None = None
    origin_hash: str = ""
    parser_name: str
    parser_version: str | None = None
    config_summary: str = ""
    result_schema_version: str
    # 结果内容哈希：同一结果重复发布时用于幂等复用，避免生成重复版本与重复分块。
    result_hash: str | None = None
    quality_status: QualityStatus
    quality_summary: str | None = None
    block_count: int = 0
    page_count: int = 0
    chunk_count: int = 0
    created_at: str
    # 结果文件相对路径：接口只暴露相对位置，不返回服务器绝对路径。
    result_json_path: str | None = None
    markdown_path: str | None = None
    # 结果文件只暴露是否可用，不暴露服务器绝对路径。
    has_structured_result: bool = False
    has_markdown: bool = False
    warnings: list[QualityWarning] = Field(default_factory=list)
    # legacy 版本由旧库迁移生成，仅保留原有分块与页码。
    is_legacy: bool = False


class IndexAttempt(BaseModel):
    """索引构建尝试：成功与失败分别记录，失败不覆盖已有成功索引。"""
    id: str
    document_id: str
    index_id: str | None = None
    target_parse_version_id: str
    status: Literal["pending", "indexing", "indexed", "failed", "superseded"]
    provider_signature: str | None = None
    source_signature: str | None = None
    source_signature_algo: str | None = None
    dimension: int | None = None
    chunk_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    started_at: str
    finished_at: str | None = None


class IndexInfo(BaseModel):
    """索引：绑定唯一解析版本；检索只读取该版本的分块。"""
    id: str | None = None
    document_id: str
    parse_version_id: str | None = None
    status: IndexStatus
    model_signature: str | None = None
    source_signature: str | None = None
    source_signature_algo: str | None = None
    dimension: int | None = None
    chunk_count: int = 0
    error: str | None = None
    created_at: str | None = None
    activated_at: str | None = None
    # 索引绑定版本是否仍等于当前预览版本；false 时页面必须提示“检索仍使用旧版本”。
    matches_active_version: bool = False
    # 当前可用索引是否为旧库迁移生成的兼容索引。
    is_legacy: bool = False
    attempts: list[IndexAttempt] = Field(default_factory=list)


class ParseTask(BaseModel):
    """持久化解析任务：本地任务状态、上游 task_id、租约与安全错误说明。"""
    id: str
    document_id: str
    status: ParseTaskStatus
    stage: ParseStage
    idempotency_key: str | None = None
    request_summary: dict[str, Any] | None = None
    upstream_task_id: str | None = None
    attempt_count: int = 0
    max_attempts: int = 1
    lease_token: str | None = None
    lease_expires_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    # 任务已产出的解析版本（成功时指向新版本；失败时为 None，旧版本不受影响）。
    result_version_id: str | None = None
    # 阶段说明用于页面展示，不表示完成百分比。
    stage_detail: str | None = None
    warnings: list[QualityWarning] = Field(default_factory=list)


class Document(BaseModel):
    """文档元数据：保留旧字段，并补充格式、版本与三类状态的摘要。

    兼容说明：status 仍是旧的单值字段，但语义调整为“内容可用性”派生值
    （parsed 表示存在活动解析版本），任务状态改由 task_status 表达，
    因此新任务失败不会把已有可检索内容标记成 failed。
    """
    id: str
    filename: str
    size: int
    created_at: str
    status: Literal["uploaded", "parsing", "parsed", "failed"]
    page_count: int = 0
    chunk_count: int = 0
    error: str | None = None
    # 新增字段均有默认值，旧客户端与旧测试仍可解析响应。
    format: DocumentFormat | None = None
    format_source: str | None = None
    active_parse_version_id: str | None = None
    active_index_id: str | None = None
    task_status: ParseTaskStatus | None = None
    task_stage: ParseStage | None = None
    quality_status: QualityStatus | None = None
    latest_task_id: str | None = None
    latest_task_error: str | None = None
    # 存在旧索引但预览已是新版本时，页面据此提示“检索仍使用旧版本”。
    index_version_mismatch: bool = False


class UploadResponse(BaseModel):
    """上传响应：保留 201 与文档结构，并补充识别到的格式与判定依据。"""
    document: Document
    format: DocumentFormat
    format_source: str
    format_note: str


class ParseRequest(BaseModel):
    """解析请求：force 表示明确重新解析；idempotency_key 用于识别重复提交。"""
    force: bool = False
    idempotency_key: str | None = Field(default=None, max_length=200)


class ParseSubmitResponse(BaseModel):
    """解析提交响应：202 表示已排队或正在执行，200 表示复用了已有有效结果。"""
    task: ParseTask | None = None
    document: Document
    reused: bool = False
    message: str


class SearchRequest(BaseModel):
    # 只检索当前文档，限制返回数量，避免一次请求取回过多原文。
    query: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    top_k: int = Field(default=5, ge=1, le=20)


class Citation(BaseModel):
    # 来源包含文档、分块、页码及引用文本；真实性需由后续业务层校验。
    document_id: str
    chunk_id: str
    page: int | None = None
    quote: str
    parse_version_id: str | None = None
    sources: list[SourceLocation] = Field(default_factory=list)


class Question(BaseModel):
    # 限制问题长度，并要求至少包含一个非空白字符。
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class Answer(BaseModel):
    # 问答响应契约：正文和支撑答案的来源列表。
    answer: str
    citations: list[Citation]


class Summary(BaseModel):
    # 摘要也保留引用，方便对照原文核实。
    summary: str
    citations: list[Citation]


class ExtractedItem(BaseModel):
    # 三类关键信息分别对应数据、结论、观点。
    kind: Literal["data", "conclusion", "viewpoint"]
    content: str
    citations: list[Citation]


class Extraction(BaseModel):
    # 一次提取可以返回多个条目，每个条目拥有自己的引用。
    items: list[ExtractedItem]
