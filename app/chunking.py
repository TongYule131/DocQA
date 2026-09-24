"""结构化分块：按标题与段落合并、表格按行组切分，并保留准确来源。

对应任务书 D3：
- 长度单位是字符数（不是 token 数），上限与 embedding 请求限制协调；
- 普通正文优先按标题与段落合并，长段落在句界或安全文本边界继续切分；
  续切只保留必要上下文，不产生仅由重叠构成的片段；
- 表格保留标题、单位、多层表头、合并单元格与注释，长表按行组切分，
  每块带必要表头与口径以及行范围；
- 单行过长也有确定处理规则与来源，不越过限制也不悄悄截断；
- JSON 是主结构，Markdown 只是派生展示文本；来源来自结构化块，不从 Markdown 反推；
- 不同时索引同一表格的整表文本、单元格子文本与重复副本。
"""
import logging
import re
from dataclasses import dataclass
from uuid import uuid4

from app.config import Settings
from app.document_normalizer import NormalizedDocument
from app.schemas import Block, Chunk, SourceLocation

logger = logging.getLogger(__name__)

# 分块算法标识：写入索引记录，便于判断旧索引由哪种规则生成。
CHUNKING_ALGO = "structured-heading-table-v2"

# 分块类型：正文 / 表格行组 / 目录 / 公式占位 / 单行超长续切。
CHUNK_TYPE_TEXT = "text"
CHUNK_TYPE_TABLE = "table_rows"
CHUNK_TYPE_INDEX = "document_index"
CHUNK_TYPE_FORMULA = "formula_placeholder"
CHUNK_TYPE_OVERFLOW = "long_line"

# 可合并进正文流的块类型：页眉页脚不参与，避免污染检索。
_FLOW_TYPES = {"paragraph", "list_item", "caption", "footnote", "code", "reference"}

# 句界：中文句号、问号、叹号、分号与英文句点、问号、叹号。
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])|(?<=[.])(?=\s|$)")
# 安全文本边界：换行、制表符与中英文标点，用于避免在词中间切断。
_SAFE_BREAK = re.compile(r"[\n\t ，、；：,;:）)】」”]")

# 表头行识别：首行或标记为 column_header 的行。
_MAX_HEADER_ROWS = 4


@dataclass
class ChunkingConfig:
    """分块参数；长度单位为字符。"""
    max_chars: int = 800
    overlap_chars: int = 120
    table_rows_per_chunk: int = 20

    @classmethod
    def from_settings(cls, settings: Settings) -> "ChunkingConfig":
        return cls(max_chars=settings.chunk_max_chars, overlap_chars=settings.chunk_overlap_chars,
                   table_rows_per_chunk=settings.table_rows_per_chunk)


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """把长文本按句界优先、安全边界其次切分。

    返回的片段拼接后覆盖全部原文；只有第一段之后才带重叠上下文，
    且每段都包含新的正文内容，不产生仅由重叠构成的片段。
    """
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    start = 0
    total = len(text)
    while start < total:
        end = min(start + max_chars, total)
        if end < total:
            window = text[start:end]
            # 优先在句界切分；找不到句界时退回安全文本边界。
            cut = None
            for match in _SENTENCE_END.finditer(window):
                if match.end() >= max_chars * 0.5:
                    cut = match.end()
            if cut is None:
                for match in _SAFE_BREAK.finditer(window):
                    if match.end() >= max_chars * 0.5:
                        cut = match.end()
            if cut is not None:
                end = start + cut
        piece = text[start:end]
        if piece.strip():
            pieces.append(piece)
        if end >= total:
            break
        # 重叠只用于保留边界上下文，步长至少为 1 个字符，避免死循环。
        step = max(1, (end - start) - overlap_chars)
        start += step
    return pieces


def _table_rows(block: Block) -> tuple[list[list[str]], list[int]]:
    """从表格块还原“行文本 + 行号”。

    三种情况必须可区分：
    - 真实空白单元格：原文即为空，渲染为（空）；
    - 跨行跨列合并占位：同一文本被上游重复输出，只保留一次，其余位置渲染为 ↳；
    - 缺失公式值：由公式告警单独标注，不在这里变成 0。
    """
    table = block.table or {}
    cells = table.get("cells") or []
    if cells:
        by_row: dict[int, list[tuple[int, str]]] = {}
        for cell in cells:
            row = cell.get("row")
            if not isinstance(row, int):
                continue
            text = (cell.get("text") or "").strip()
            col = cell.get("col") if isinstance(cell.get("col"), int) else 0
            by_row.setdefault(row, []).append((col, text))
        rows: list[list[str]] = []
        numbers: list[int] = []
        for row_index in sorted(by_row):
            entries = sorted(by_row[row_index])
            values: list[str] = []
            for _col, text in entries:
                if values and text and values[-1] == text:
                    # 合并单元格的重复文本：标记为占位，不再重复输出内容。
                    values.append("↳")
                    continue
                values.append(text)
            rows.append(values)
            numbers.append(row_index)
        return rows, numbers
    # 没有单元格结构时退回按行拆分表格文本。
    lines = [line for line in (block.text or "").splitlines() if line.strip()]
    return [[line] for line in lines], list(range(len(lines)))


def _header_rows(block: Block, rows: list[list[str]]) -> int:
    """识别表头行数：标记为 column_header 的行 + 可能的第二层表头。"""
    table = block.table or {}
    cells = table.get("cells") or []
    if not cells:
        return 1 if rows else 0
    header_row_indices = {
        cell.get("row") for cell in cells
        if cell.get("column_header") and isinstance(cell.get("row"), int)
    }
    if not header_row_indices:
        return 1 if rows else 0
    return min(max(header_row_indices) + 1, _MAX_HEADER_ROWS, len(rows))


def _table_context(block: Block) -> str:
    """表格口径：标题、单位、工作表名、表编号与公式缓存状态，随每个表格分块保留。"""
    table = block.table or {}
    parts: list[str] = []
    captions = [str(item).strip() for item in (table.get("captions") or []) if str(item).strip()]
    if captions:
        parts.append("表格标题：" + " / ".join(captions))
    if table.get("footnotes"):
        parts.append("注释：" + " / ".join(str(note) for note in table["footnotes"]))
    if block.sheet_name:
        parts.append(f"工作表：{block.sheet_name}")
    if table.get("table_no"):
        parts.append(f"表编号：{table['table_no']}")
    if block.heading_path:
        parts.append(f"章节：{block.heading_path}")
    if table.get("cell_range"):
        parts.append(f"单元格范围：{table['cell_range']}")
    formulas = table.get("formulas") or {}
    if formulas:
        # 公式表达式与“文件保存的缓存值”必须进入检索文本，缓存缺失时明确标注，
        # 不能变成 0，也不能让表达式在分块后丢失。
        entries = []
        for coordinate, info in sorted(formulas.items()):
            cache = info.get("cached_value")
            cached = str(cache) if info.get("has_cache") else "缺失（不能当作 0）"
            entries.append(f"{coordinate}={info.get('formula')}（缓存：{cached}）")
        parts.append("公式：" + "；".join(entries))
    return "；".join(parts)


def _source_summary(sources: list[SourceLocation]) -> list[SourceLocation]:
    return [SourceLocation(**source.model_dump()) for source in sources]


def chunk_document(document: NormalizedDocument, config: ChunkingConfig) -> list[Chunk]:
    """把规范化结果切分为检索分块。

    分块顺序显式写入 order_index；块 ID 随机生成但内容与来源可追溯到解析版本。
    """
    if config.max_chars <= 0 or not 0 <= config.overlap_chars < config.max_chars:
        raise ValueError("分块长度必须大于 0，且重叠长度必须小于分块长度")
    chunks: list[Chunk] = []
    order = 0

    def add(block: Block, text: str, chunk_type: str, page: int | None,
            sources: list[SourceLocation], heading_path: str | None, note: str | None = None) -> None:
        nonlocal order
        payload = text.strip()
        if not payload:
            return
        if len(payload) > config.max_chars:
            # 标题/公式等特殊节点也必须遵守长度上限，不能只限制普通正文。
            for part in _split_long_text(payload, config.max_chars, config.overlap_chars):
                add(block, part, chunk_type, page, sources, heading_path, note)
            return
        final_sources = _source_summary(sources)
        if note:
            # 附加说明写在来源 note 上，便于页面解释该块的切分方式。
            if final_sources:
                final_sources[0].note = (final_sources[0].note + "；" if final_sources[0].note else "") + note
        chunks.append(Chunk(
            id=uuid4().hex, document_id=document.document_id, page=page, text=payload,
            parse_version_id=None, order_index=order, block_id=block.id, chunk_type=chunk_type,
            heading_path=heading_path, sources=final_sources,
        ))
        order += 1

    # ---------------- 正文流：按标题分组，段落合并到长度上限 ----------------
    flow_blocks: list[Block] = []

    def flush_flow() -> None:
        """把累积的正文块合并为一个或多个分块，长文本在句界续切。"""
        if not flow_blocks:
            return
        # 文本和来源一起累积；每次切块只附带该片段实际覆盖的来源。
        buffer = ""
        sources: list[SourceLocation] = []
        first = flow_blocks[0]

        def emit():
            if buffer:
                add(first, buffer, CHUNK_TYPE_TEXT, _first_page(sources), sources, first.heading_path)

        for block in flow_blocks:
            text = (block.text or "").strip()
            if not text:
                continue
            candidate = f"{buffer}\n{text}" if buffer else text
            if len(candidate) <= config.max_chars:
                if not buffer:
                    first = block
                buffer = candidate
                sources.extend(block.sources)
                continue
            emit()
            buffer = ""
            sources = []
            first = block
            if len(text) > config.max_chars:
                for piece in _split_long_text(text, config.max_chars, config.overlap_chars):
                    add(block, piece, CHUNK_TYPE_TEXT, _first_page(block.sources), block.sources,
                        block.heading_path)
            else:
                buffer = text
                sources = list(block.sources)
        emit()
        flow_blocks.clear()

    for block in document.blocks:
        if block.block_type in _FLOW_TYPES:
            # 标题变化时先落盘上一组，保证标题路径与分块内容一致。
            if flow_blocks and block.heading_path != flow_blocks[0].heading_path:
                flush_flow()
            flow_blocks.append(block)
            continue
        if block.block_type in {"title", "section_header"}:
            flush_flow()
            # 标题单独成块：保证检索能命中章节名并携带标题路径。
            add(block, block.text, CHUNK_TYPE_TEXT, _first_page(block.sources),
                block.sources, block.heading_path)
            continue
        flush_flow()
        if block.block_type in {"table", "document_index"}:
            _chunk_table(block, config, add, document)
            continue
        if block.block_type == "formula":
            # 空公式节点保留来源与占位文本，避免被当成正文事实。
            text = (block.text or "").strip() or "（公式内容缺失，无法作为可靠事实）"
            add(block, text, CHUNK_TYPE_FORMULA, _first_page(block.sources), block.sources,
                block.heading_path, note="公式内容缺失告警对应的占位块")
            continue
        if block.block_type in {"picture", "page_header", "page_footer"}:
            # 页眉页脚与图片不进入检索分块；它们仍保留在预览中。
            continue
        if (block.text or "").strip():
            add(block, block.text, CHUNK_TYPE_TEXT, _first_page(block.sources), block.sources,
                block.heading_path)
    flush_flow()
    return chunks


def _first_page(sources: list[SourceLocation]) -> int | None:
    for source in sources:
        if source.page:
            return source.page
    return None


def _chunk_table(block: Block, config: ChunkingConfig, add, document: NormalizedDocument) -> None:
    """表格分块：长表按行组切分，每块携带表头、口径与行范围。"""
    rows, row_numbers = _table_rows(block)
    context = _table_context(block)
    chunk_type = CHUNK_TYPE_INDEX if block.block_type == "document_index" else CHUNK_TYPE_TABLE
    if not rows:
        return
    header_count = _header_rows(block, rows)
    header_rows = rows[:header_count]
    body_rows = rows[header_count:]
    body_numbers = row_numbers[header_count:]
    if not body_rows:
        # 只有表头：整体成块，避免丢失标题与单位。
        text = "\n".join(part for part in [context, _render_rows(header_rows)] if part)
        add(block, text, chunk_type, _first_page(block.sources), block.sources, block.heading_path)
        return
    group_size = max(1, config.table_rows_per_chunk)
    for start in range(0, len(body_rows), group_size):
        group = body_rows[start:start + group_size]
        numbers = body_numbers[start:start + group_size]
        if not numbers:
            continue
        row_range = f"第 {numbers[0] + 1}-{numbers[-1] + 1} 行（表内行号，含表头 {header_count} 行）"
        parts = [context, f"行范围：{row_range}"]
        if header_rows:
            parts.append("表头：" + _render_rows(header_rows))
        parts.append(_render_rows(group))
        text = "\n".join(part for part in parts if part)
        if len(text) <= config.max_chars:
            add(block, text, chunk_type, _first_page(block.sources), block.sources,
                block.heading_path, note=f"表格行组，行范围 {row_range}")
            continue
        # 行组超过长度上限：先按“每行一块”细分，保证每个分块都带表头与口径；
        # 单行本身仍超限时按句界/安全边界续切，并为每段重新附上表头与行范围，
        # 避免续切片段丢失表头、单位或行范围上下文，也不悄悄截断。
        for row, number in zip(group, numbers):
            single_range = f"第 {number + 1} 行（表内行号，含表头 {header_count} 行）"
            prefix_parts = [context, f"行范围：{single_range}"]
            if header_rows:
                prefix_parts.append("表头：" + _render_rows(header_rows))
            prefix = "\n".join(part for part in prefix_parts if part)
            data = _render_rows([row])
            single_text = f"{prefix}\n{data}" if prefix else data
            if len(single_text) <= config.max_chars:
                add(block, single_text, chunk_type, _first_page(block.sources), block.sources,
                    block.heading_path, note=f"表格单行（原行组超长拆分），行范围 {single_range}")
                continue
            # 为上下文预留空间，剩余额度用于切分该行数据本身。
            if len(prefix) >= config.max_chars - 32:
                # 完整口径本身超限时不能截掉单位/条件，也不能悄悄生成超长请求。
                # 保守拒绝本次解析，旧版本仍保留；用户可调整块上限后再解析。
                raise ValueError("表格标题、表头或注释超过分块容量，请增大 DOCQA_CHUNK_MAX_CHARS 后重新解析")
            budget = config.max_chars - len(prefix) - 1
            for index, piece in enumerate(_split_long_text(data, budget, min(config.overlap_chars, budget // 4))):
                text = f"{prefix}\n{piece}" if prefix else piece
                add(block, text, CHUNK_TYPE_OVERFLOW,
                    _first_page(block.sources), block.sources, block.heading_path,
                    note=f"该表格行超过长度上限，按句界/安全边界续切第 {index + 1} 段；行范围 {single_range}")


def _render_rows(rows: list[list[str]]) -> str:
    """把行渲染为检索文本；区分真实空白（空）与合并占位（↳）。"""
    lines = []
    for row in rows:
        values = [value if value != "" else "（空）" for value in row]
        lines.append(" | ".join(values))
    return "\n".join(lines)
