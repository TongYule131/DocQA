# Pydantic 数据契约：用于请求校验、响应序列化和接口文档生成。
#
# 本阶段在原有契约上补充三组彼此独立的状态（对应任务书 3.1）：
#   1. 任务状态 ParseTask.status（本次解析是否排队/执行/失败/完成）；
#   2. 内容质量 ParseVersion.quality_status + QualityWarning（结构是否可用、有何告警）；
#   3. 索引状态 IndexInfo.status（哪个解析版本的向量可用）。
# 任何一组状态都不再用 documents.status 一个枚举表达。
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    """问答请求：只接受问题文本，明确拒绝额外字段。

    调用方不能通过请求提交自定义 system prompt、上下文、chunk、模型端点或文档路径；
    top_k 与预算均由服务端配置，不作为本次请求参数。
    """
    model_config = ConfigDict(extra="forbid")

    # 限制问题长度，并要求至少包含一个非空白字符。
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class Answer(BaseModel):
    # 问答响应契约：正文和支撑答案的来源列表。
    answer: str
    citations: list[Citation]


# ----------------------------------------------------------------------
# RAG 问答契约（第一版：单文档、单轮、非流式）
# ----------------------------------------------------------------------
RagStatus = Literal["answered", "clarification_needed", "insufficient_evidence"]


class RagFact(BaseModel):
    """一条已通过校验的简短事实；refs 由后端从已校验结构复制，编号由服务端分配。"""
    text: str
    refs: list[int]


class RagCitation(BaseModel):
    """引用卡片：引述来自通过校验的模型引述，定位信息全部来自本次证据映射。

    sources 完整保留已有来源结构（含 bbox、章节/表号、工作表/单元格、TXT 行号），
    不把 Office 或 TXT 的来源一律精简成 PDF 页码。
    """
    reference_id: int
    document_id: str
    chunk_id: str
    parse_version_id: str | None = None
    page: int | None = None
    quote: str
    sources: list[SourceLocation] = Field(default_factory=list)


class RagRetrievalStats(BaseModel):
    """本次检索统计：字符预算不是 token 预算，不在此宣称精确 token 限制。"""
    candidate_count: int
    selected_count: int
    context_chars: int
    truncated: bool


class RagTimings(BaseModel):
    """真实非负耗时；没有调用生成时 generation 为 0，不编造 token 用量。"""
    retrieval: int
    generation: int
    total: int


class RagAnswer(BaseModel):
    """RAG 问答响应。

    保留顶层 answer 与 citations，并补充状态、版本、检索统计、告警与耗时。
    conclusion/explanation 是已通过校验的展示结构，与 answer 的 Markdown 内容一致；
    不返回未经校验的原始模型 JSON。
    """
    answer_id: str
    status: RagStatus
    answer: str
    clarification_questions: list[str] = Field(default_factory=list)
    citations: list[RagCitation] = Field(default_factory=list)
    conclusion: list[RagFact] = Field(default_factory=list)
    explanation: list[RagFact] = Field(default_factory=list)
    document_id: str
    index_id: str | None = None
    parse_version_id: str | None = None
    is_old_version: bool = False
    is_current_index: bool = False
    prompt_version: str
    retrieval: RagRetrievalStats
    quality_warnings: list[QualityWarning] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    timings_ms: RagTimings


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


# ----------------------------------------------------------------------
# 分析任务契约（摘要 / 信息提取）
#
# 与 RAG 问答的三处关键区别（都直接影响可信度，必须显式表达）：
#   1. 摘要与提取**基于解析版本**，不依赖 embedding 索引；因此输入是完整的
#      合格正文／表格清单，而不是检索 top-k。
#   2. 结果持久化：任务、尝试、分批步骤、调用账本与已校验结果全部落库；
#      HTTP 响应与页面展示都只使用**已校验结构**，绝不回显原始模型正文。
#   3. 覆盖范围与任务状态分开：任务 succeeded 只表示本次批次全部处理，
#      覆盖字段仍要如实给出“总输入单元 / 已处理 / 未处理及原因”。
# ----------------------------------------------------------------------

# 分析任务顶层状态：与解析任务保持同一套取值，便于页面复用。
AnalysisJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "needs_attention"]
# 处理阶段用于展示“正在做什么”，不代表完成百分比。
AnalysisStage = Literal["queued", "planning", "generating", "reducing", "validating", "saving", "done"]
AnalysisKind = Literal["extraction", "summary"]

# 提取条目的类别：数据 / 结论 / 观点。
ExtractionItemKind = Literal["data", "conclusion", "viewpoint"]

# 是否存在某类内容：present 有提取项；none 已覆盖输入但没有该类内容；
# not_applicable 明确不适用（例如表格口径下不给出观点）；unprocessed 未处理。
SectionStatus = Literal["present", "none", "not_applicable", "unprocessed"]


class AnalysisPlanBatch(BaseModel):
    """一个输入批次：只描述本地规划结果，不含模型输出。"""
    batch_id: str
    order_index: int
    unit_ids: list[str]
    unit_count: int
    chars: int
    message_chars: int


class AnalysisPlanCoverage(BaseModel):
    """规划阶段的覆盖口径。

    total_units 只统计“可分析单元”（正文／表格／公式占位／图表标题／引用），
    页眉、页脚、图片与空白块属于 excluded_units，并单独给出原因计数。
    """
    total_units: int
    planned_units: int
    total_chars: int
    excluded_units: int
    excluded_reasons: dict[str, int] = Field(default_factory=dict)
    batch_count: int


class AnalysisPlanLimits(BaseModel):
    """本次规划使用的配置上界，页面据此说明“为什么不可执行”。"""
    max_requests: int
    batch_max_chars: int
    reduce_max_chars: int
    input_max_chars: int
    max_document_chars: int
    max_items_per_batch: int
    max_items_total: int


class AnalysisPlan(BaseModel):
    """POST /analysis-plan 的响应：零外部调用，只做本地输入规划。"""
    document_id: str
    parse_version_id: str
    kind: AnalysisKind
    prompt_version: str
    protocol_version: str
    plan_fingerprint: str
    is_active_version: bool
    coverage: AnalysisPlanCoverage
    limits: AnalysisPlanLimits
    batches: list[AnalysisPlanBatch] = Field(default_factory=list)
    # 汇总阶段是否需要一次独立调用（多批摘要需要；单批提取不需要）。
    reduce_required: bool = False
    # 规划出的请求数上界：分批数 + （需要时）1 次汇总，全部计入任务预算。
    request_upper_bound: int = 0
    executable: bool = False
    limitations: list[str] = Field(default_factory=list)
    # 不可执行时的稳定原因码：文档过大、批次预算过小、请求数超预算等。
    blocked_reason: str | None = None


class AnalysisJobRequest(BaseModel):
    """创建分析任务的最小请求体。

    只接受解析版本、计划指纹与幂等键；审核类型由 summary／extract 入口决定。
    模型端点、system 提示、证据正文与文件路径都不允许由前端提交。
    """
    model_config = ConfigDict(extra="forbid")

    parse_version_id: str | None = Field(default=None, max_length=200)
    plan_fingerprint: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=200)
    # 明确重新生成：必须由用户操作产生，会创建新的结果版本。
    regenerate: bool = False


class AnalysisJob(BaseModel):
    """分析任务：状态、阶段、进度、预算与结果入口。"""
    id: str
    document_id: str
    parse_version_id: str
    kind: AnalysisKind
    status: AnalysisJobStatus
    stage: AnalysisStage
    stage_detail: str | None = None
    idempotency_key: str | None = None
    plan_fingerprint: str | None = None
    retry_of_document: str | None = None
    request_upper_bound: int = 0
    max_requests: int = 0
    requests_used: int = 0
    steps_total: int = 0
    steps_completed: int = 0
    result_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    # 执行令牌只在 worker 内部使用；出现在契约里是为了让离线测试能断言
    # “过期令牌不能继续发请求或发布结果”，页面不会使用该字段。
    lease_token: str | None = None
    lease_expires_at: str | None = None
    attempt_count: int = 0
    # 版本与配置指纹（不含密钥），用于判断同版本同配置能否复用已有成功结果。
    prompt_version: str = ""
    protocol_version: str = ""
    model_signature: str = ""
    # 覆盖口径：与任务状态分开，失败任务也可显示已完成批次进度。
    coverage: dict[str, Any] | None = None
    limitations: list[str] = Field(default_factory=list)
    # 该文档是否已有同版本同配置的成功结果（页面据此提示“可查看历史结果”）。
    has_result: bool = False


class AnalysisCallEntry(BaseModel):
    """调用账本条目：证明“每次外部请求前已预扣预算并写入调用意图”。"""
    id: str
    job_id: str
    step_id: str | None = None
    role: Literal["batch", "reduce"]
    sequence_no: int
    status: Literal["intent", "succeeded", "failed", "uncertain", "skipped"]
    error_code: str | None = None
    started_at: str
    settled_at: str | None = None
    elapsed_ms: int | None = None


class AnalysisStepInfo(BaseModel):
    """已校验的分批／汇总检查点。"""
    id: str
    job_id: str
    role: Literal["batch", "reduce"]
    order_index: int
    batch_id: str | None = None
    status: Literal["pending", "succeeded", "failed"]
    unit_ids: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class AnalysisCitation(BaseModel):
    """引用卡片：全部字段来自服务端来源映射与已校验引述。

    服务端只把“解析版本内的单元编号”交给模型；块 ID、页码、章节、工作表与
    单元格范围都由后端从本次输入映射复制，模型既看不到也无法伪造。
    定位信息缺失时保持为空或 null，绝不代为编造页码或坐标。
    """
    reference_id: int
    # 块 ID 由服务端映射给出；定位信息缺失时为空串，不编造。
    block_id: str = ""
    source_index: int | None = None
    block_type: str | None = None
    quote: str
    sources: list[SourceLocation] = Field(default_factory=list)
    # 引述在原文中的偏移只声明“已校验的连续子串位置”，不声称已实现字符级高亮。
    char_start: int | None = None
    char_end: int | None = None


class AnalysisExtractionItem(BaseModel):
    """一条数据／结论／观点。

    可选字段没有原文依据时为 null，明确区分“未提及”与“明确不适用”；
    数值一律保留原文文本与单位，绝不换算、不补 0、不做数值推导。
    """
    item_id: str
    kind: ExtractionItemKind
    content: str
    name: str | None = None
    value_text: str | None = None
    unit: str | None = None
    period: str | None = None
    subject: str | None = None
    scope: str | None = None
    refs: list[int] = Field(default_factory=list)


class AnalysisExtractionResult(BaseModel):
    """信息提取的已校验结果。"""
    items: list[AnalysisExtractionItem] = Field(default_factory=list)
    sections: dict[str, SectionStatus] = Field(default_factory=dict)
    citations: list[AnalysisCitation] = Field(default_factory=list)


class AnalysisSummaryPoint(BaseModel):
    """摘要要点／例外：各自携带引用编号。"""
    text: str
    refs: list[int] = Field(default_factory=list)


class AnalysisSummaryResult(BaseModel):
    """文档摘要的已校验结果。"""
    topic_overview: str = ""
    main_points: list[AnalysisSummaryPoint] = Field(default_factory=list)
    exceptions: list[AnalysisSummaryPoint] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    citations: list[AnalysisCitation] = Field(default_factory=list)


class AnalysisCoverage(BaseModel):
    """已发布结果的覆盖口径（不是 OCR 正确率，也不是任意全文理解率）。"""
    total_units: int = 0
    processed_units: int = 0
    unresolved_units: int = 0
    unresolved_reasons: dict[str, int] = Field(default_factory=dict)
    excluded_units: int = 0
    excluded_reasons: dict[str, int] = Field(default_factory=dict)
    batch_total: int = 0
    batch_completed: int = 0
    reduce_completed: bool = False
    complete: bool = False
    # 中间摘要不是原文：这里记录最终引用是否全部回落到原文单元。
    resolved_original_refs: int = 0
    unresolved_original_refs: int = 0


class AnalysisResult(BaseModel):
    """已校验结果：页面展示与导出都从这里生成。"""
    id: str
    job_id: str
    document_id: str
    parse_version_id: str
    kind: AnalysisKind
    is_active_version: bool = False
    coverage: AnalysisCoverage
    quality_warnings: list[QualityWarning] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    prompt_version: str
    protocol_version: str
    model_signature: str = ""
    parse_parser_name: str | None = None
    parse_quality_status: QualityStatus | None = None
    requests_used: int = 0
    created_at: str
    extraction: AnalysisExtractionResult | None = None
    summary: AnalysisSummaryResult | None = None


class AnalysisResultSummary(BaseModel):
    """结果历史列表项：不含正文，避免列表接口返回大量内容。"""
    id: str
    job_id: str
    document_id: str
    parse_version_id: str
    kind: AnalysisKind
    is_active_version: bool = False
    is_current_version: bool = False
    complete: bool = False
    coverage: AnalysisCoverage
    prompt_version: str
    protocol_version: str
    requests_used: int = 0
    created_at: str


class AnalysisSubmitResponse(BaseModel):
    """摘要／提取提交响应：202 表示已排队或正在执行，200 表示复用已有成功结果。"""
    job: AnalysisJob
    document: Document
    reused: bool = False
    regenerated: bool = False
    message: str
