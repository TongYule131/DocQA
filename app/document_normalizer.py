"""结构规范化：把 Docling 原始结果转换为与上游无关的业务结构。

对应任务书 D1/D2：
- 按 Docling 的 body/children 层级遍历阅读顺序，解析 $ref 引用，
  不直接遍历 texts 后再追加 tables，避免表格子文本与整表重复索引；
- PDF 保存一基页号、原始 bbox、坐标原点与单位，空白页继续占原页号；
- DOCX 由标题父子结构生成章节路径，表格保存表编号与行列，无真实页码时 page=None；
- XLSX 读取 sheet 分组名，把表来源网格起点与表内偏移组合成真实单元格坐标；
- TXT 保留旧逻辑页并新增行范围，说明其页码不是物理 PDF 页码；
- 未识别的关键结构类型或不合法来源必须显式告警，不静默抛弃后标记完整成功；
- 质量告警包括公式内容缺失、公式缓存缺失、目录点线污染、空白页、图表未语义解析等。
"""
import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from app.schemas import Block, QualityWarning, SourceLocation

logger = logging.getLogger(__name__)

# 结果结构版本：Docling 结构变化或规范化规则调整时递增，便于识别历史版本产物。
RESULT_SCHEMA_VERSION = "docqa-normalized-3"

# Docling 文本标签到业务块类型的映射；未识别的标签会显式告警。
_LABEL_TO_TYPE = {
    "title": "title",
    "section_header": "section_header",
    "text": "paragraph",
    "paragraph": "paragraph",
    "list_item": "list_item",
    "caption": "caption",
    "table_caption": "caption",
    "footnote": "footnote",
    "page_header": "page_header",
    "page_footer": "page_footer",
    "formula": "formula",
    "code": "code",
    "reference": "reference",
    "checkbox_selected": "list_item",
    "checkbox_unselected": "list_item",
    "document_index": "document_index",
}

# 表格标签：document_index 是目录（点线引导），不是数据表。
_TABLE_LABEL_TYPE = {
    "table": "table",
    "document_index": "document_index",
}

# 目录点线：仅在“目录上下文”中保守清理，不全局删除点号、连字符或百分号。
# 实测上游会使用半角点（.）、全角点（．U+FF0E）、中点（·）与省略号（…）作引导符，
# 因此必须一并覆盖，否则真实样本的目录仍会保留大段点线污染检索文本。
_DOT_LEADER = re.compile(r"[.·・．。…]{2,}\s*")
# 目录行末尾的页码（点线后的数字）保留，其余多余空白折叠。
_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")


@dataclass
class NormalizedDocument:
    """规范化结果：有序块、来源、质量告警与统计。"""
    document_id: str
    format: str
    page_count: int
    blocks: list[Block] = field(default_factory=list)
    warnings: list[QualityWarning] = field(default_factory=list)
    parser_name: str = "docling"
    parser_version: str | None = None
    # 无来源的块数量等统计用于诊断，不作为质量结论。
    stats: dict[str, Any] = field(default_factory=dict)

    def text_length(self) -> int:
        return sum(len(block.text) for block in self.blocks)

    def indexable_blocks(self) -> list[Block]:
        """可索引块：排除页眉页脚与纯占位内容；页眉页脚仍保留在预览中。"""
        skip = {"page_header", "page_footer"}
        return [block for block in self.blocks
                if block.block_type not in skip and (block.text.strip() or block.table)]


def ref_key(node: dict[str, Any] | None) -> str | None:
    """读取 Docling 的 $ref 引用值；不把 self_ref 当作跨次解析稳定 ID。"""
    if not isinstance(node, dict):
        return None
    value = node.get("$ref")
    return value if isinstance(value, str) else None


def _index_by_ref(items: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("self_ref"), str):
                result[item["self_ref"]] = item
    return result


def _prov_to_sources(prov: Any, fmt: str, section_path: str | None,
                     sheet_name: str | None = None) -> list[SourceLocation]:
    """把 Docling 的 prov 列表转换为来源记录。

    PDF 的 prov 带 page_no 与 bbox（含 coord_origin）；DOCX/XLSX 的 prov 可能为空，
    此时不伪造页码，只保留节点引用与章节/工作表信息。
    """
    sources: list[SourceLocation] = []
    if isinstance(prov, list):
        for item in prov:
            if not isinstance(item, dict):
                continue
            bbox = item.get("bbox")
            page_no = item.get("page_no")
            coord_origin = bbox.get("coord_origin") if isinstance(bbox, dict) else None
            charspan = item.get("charspan")
            sources.append(SourceLocation(
                format=fmt,
                page=page_no if fmt in {"pdf", "txt"} and isinstance(page_no, int) else None,
                bbox=bbox if isinstance(bbox, dict) else None,
                # PDF 的 bbox 单位是 PDF 点（1/72 英寸），原点由上游给出（TOPLEFT/BOTTOMLEFT）。
                coord_origin=coord_origin if isinstance(coord_origin, str) else None,
                coord_unit="pt" if isinstance(bbox, dict) and fmt == "pdf" else None,
                section_path=section_path,
                sheet_name=sheet_name,
                char_start=charspan[0] if isinstance(charspan, list) and len(charspan) == 2 else None,
                char_end=charspan[1] if isinstance(charspan, list) and len(charspan) == 2 else None,
            ))
    if not sources and fmt in {"docx", "xlsx"}:
        sources.append(SourceLocation(format=fmt, section_path=section_path, sheet_name=sheet_name))
    return sources


def _extract_heading_path(node: dict[str, Any], index: dict[str, dict[str, Any]]) -> str | None:
    """由标题父子结构生成章节路径。

    DOCX 的 section_header 通过 parent 引用形成层级；样本只有一级标题，
    这里按父链递归收集所有 section_header/title 祖先，支持任意深度。
    """
    names: list[str] = []
    seen: set[str] = set()
    parent_ref = ref_key(node.get("parent"))
    guard = 0
    while parent_ref and parent_ref not in seen and guard < 32:
        seen.add(parent_ref)
        guard += 1
        parent = index.get(parent_ref)
        if parent is None:
            break
        label = parent.get("label")
        if label in {"section_header", "title"}:
            text = (parent.get("text") or "").strip()
            if text:
                names.append(text)
        parent_ref = ref_key(parent.get("parent"))
    if not names:
        return None
    # 父链是自下而上收集的，反转后得到从顶层到当前章节的路径。
    return " / ".join(reversed(names))


def _clean_index_text(text: str) -> str:
    """目录上下文的保守清理：只压缩点线引导符与重复空白。

    不删除单个点号、连字符或百分号，避免破坏小数、负数与百分比。
    """
    cleaned = _DOT_LEADER.sub(" ", text)
    return _MULTI_SPACE.sub(" ", cleaned).strip()


# 长点线：仅在文本块中出现 6 个及以上连续点号（含全角点、中点、省略号）时才清理。
# 该规则不会影响小数（1.9）、省略号（……）与单个句号；实测目录页会出现
# “........................................ 308” 这类独立点线块，需要一并处理。
_LONG_DOT_RUN = re.compile(r"[.·・．。…]{6,}")


def _clean_dot_leader_runs(text: str) -> tuple[str, int]:
    """清理普通文本块中的长点线，返回 (清理后的文本, 清理次数)。

    只做“点线 → 空格”的替换，保留其后的页码，不做任何数值或标点改写。
    """
    matches = _LONG_DOT_RUN.findall(text)
    if not matches:
        return text, 0
    return _MULTI_SPACE.sub(" ", _LONG_DOT_RUN.sub(" ", text)).strip(), len(matches)


def _cell_text(cell: dict[str, Any]) -> str:
    value = cell.get("text")
    return value if isinstance(value, str) else ""


def _table_to_dict(table: dict[str, Any]) -> dict[str, Any]:
    """把 Docling 表格转换为业务表格结构，保留合并信息与表头标记。"""
    data = table.get("data") or {}
    cells = data.get("table_cells") or []
    grid = data.get("grid") or []
    return {
        "num_rows": data.get("num_rows"),
        "num_cols": data.get("num_cols"),
        "orientation": data.get("orientation"),
        "cells": [
            {
                "text": _cell_text(cell),
                "row": cell.get("start_row_offset_idx"),
                "col": cell.get("start_col_offset_idx"),
                "row_span": cell.get("row_span", 1),
                "col_span": cell.get("col_span", 1),
                "column_header": bool(cell.get("column_header")),
                "row_header": bool(cell.get("row_header")),
                "row_section": bool(cell.get("row_section")),
                # bbox 为空表示该单元格没有独立版面框（Office 常见），不代表内容缺失。
                "bbox": cell.get("bbox"),
            }
            for cell in cells if isinstance(cell, dict)
        ],
        # 网格用于区分“真实空白单元格”与“跨行跨列合并占位”。
        "grid_has_cells": [len(row) for row in grid] if isinstance(grid, list) else [],
    }


def _table_plain_text(table: dict[str, Any]) -> str:
    """表格的检索文本：按行输出，保留合并与表头信息，供分块时使用。"""
    data = table.get("data") or {}
    grid = data.get("grid")
    lines: list[str] = []
    if isinstance(grid, list):
        for row in grid:
            if not isinstance(row, list):
                continue
            values: list[str] = []
            for cell in row:
                if not isinstance(cell, dict):
                    continue
                text = _cell_text(cell).strip()
                # 合并单元格的重复占位不重复输出文本，避免放大内容。
                if values and text and values[-1] == text:
                    continue
                values.append(text)
            lines.append(" | ".join(values))
    if not lines:
        cells = data.get("table_cells") or []
        lines = [(_cell_text(cell) or "") for cell in cells if isinstance(cell, dict)]
    return "\n".join(line for line in lines if line.strip())


def normalize_docling_result(document_id: str, payload: dict[str, Any], fmt: str,
                             *, parser_version: str | None = None,
                             page_count_hint: int | None = None) -> NormalizedDocument:
    """把 Docling 结构化结果规范化为业务结构。"""
    if not isinstance(payload, dict):
        raise ValueError("规范化输入必须是结构化结果对象")
    texts = _index_by_ref(payload.get("texts"))
    tables = _index_by_ref(payload.get("tables"))
    groups = _index_by_ref(payload.get("groups"))
    pictures = _index_by_ref(payload.get("pictures"))
    pages = payload.get("pages") if isinstance(payload.get("pages"), dict) else {}
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}

    document = NormalizedDocument(
        document_id=document_id, format=fmt, parser_version=parser_version,
        page_count=len(pages) if pages else (page_count_hint or 0),
    )
    warnings = document.warnings
    blocks: list[Block] = []
    # XLSX：sheet 分组名 -> 工作表名，供表格与文本块标注来源工作表。
    sheet_by_group: dict[str, str] = {}
    for ref, group in groups.items():
        if group.get("label") == "sheet" and group.get("name"):
            sheet_by_group[ref] = str(group["name"])
    # 分组自身也保留工作表名（表格块可能在分组内被递归处理）。
    group_sheet: dict[str, str] = dict(sheet_by_group)

    # 收集表格富文本单元格的引用：这些文本已由表格结构表达，不能再单独成块。
    table_child_refs: set[str] = set()
    for group_ref, group in groups.items():
        parent_ref = ref_key(group.get("parent"))
        if parent_ref and parent_ref in tables:
            for child in group.get("children") or []:
                child_ref = ref_key(child)
                if child_ref:
                    table_child_refs.add(child_ref)
    for table in tables.values():
        for child in table.get("children") or []:
            child_ref = ref_key(child)
            if child_ref:
                table_child_refs.add(child_ref)

    children = body.get("children") or []
    if not isinstance(children, list) or not children:
        raise ValueError("结构化结果缺少 body.children，无法按阅读顺序遍历")

    def current_sheet(node: dict[str, Any]) -> str | None:
        """沿父链找到最近的 sheet 分组名。

        XLSX 的表格父节点就是 sheet 分组，因此先看自身的 self_ref 是否属于工作表分组，
        再看父链；否则表格块会丢失工作表来源（Markdown 不保留工作表标签）。
        """
        own_ref = node.get("self_ref")
        if isinstance(own_ref, str) and own_ref in group_sheet:
            return group_sheet[own_ref]
        parent_ref = ref_key(node.get("parent"))
        guard = 0
        while parent_ref and guard < 32:
            guard += 1
            if parent_ref in sheet_by_group:
                return sheet_by_group[parent_ref]
            parent = groups.get(parent_ref)
            if parent is None:
                break
            parent_ref = ref_key(parent.get("parent"))
        return None

    order = 0
    table_no = 0
    # 使用队列展开 body 顺序：分组（如 XLSX 的 sheet、PDF 的 list）在遇到时把子节点
    # 追加到队首，从而在保持阅读顺序的同时递归处理分组内部的表格与文本。
    queue: list[str] = []
    for child in children:
        ref = ref_key(child)
        if ref:
            queue.append(ref)
    guard = 0
    # 已处理过的引用：Docling 可能同时把节点挂在父节点和 body.children 下，
    # 必须去重，避免同一内容被索引两次。
    processed: set[str] = set()
    while queue and guard < 200000:
        guard += 1
        child_ref = queue.pop(0)
        if child_ref in processed:
            continue
        processed.add(child_ref)
        if child_ref in texts:
            node = texts[child_ref]
            if child_ref in table_child_refs:
                # 表格单元格富文本：跳过，避免与整表重复索引（已在告警中统计）。
                continue
            label = str(node.get("label") or "text")
            block_type = _LABEL_TO_TYPE.get(label)
            if block_type is None:
                warnings.append(QualityWarning(
                    code="unknown_structure_type", severity="warning", scope="block",
                    message=f"遇到未识别的结构类型「{label}」，已按正文保留并提示人工核对",
                    detail=f"node={child_ref}",
                ))
                block_type = "paragraph"
            text = node.get("text")
            text = text if isinstance(text, str) else ""
            section_path = _extract_heading_path(node, texts)
            sheet_name = current_sheet(node)
            if block_type in {"paragraph", "list_item", "caption", "footnote"}:
                # 目录页的独立点线块：只在出现长点线时清理，并留痕说明。
                text, cleaned_runs = _clean_dot_leader_runs(text)
                if cleaned_runs:
                    warnings.append(QualityWarning(
                        code="toc_dot_leaders", severity="info", scope="block",
                        page=node.get("prov", [{}])[0].get("page_no") if node.get("prov") else None,
                        message="该文本块含目录点线引导符，已按目录上下文保守清理；"
                                "页码对应关系仍需对照原件核对",
                        source=_first_source(node.get("prov"), fmt, section_path, sheet_name),
                        detail=f"node={child_ref} 清理 {cleaned_runs} 处",
                    ))
            if block_type == "formula" and not text.strip():
                # 公式节点内容为空：保留来源并告警，不填补公式内容。
                warnings.append(QualityWarning(
                    code="formula_content_missing", severity="warning", scope="block",
                    message="公式节点没有可用内容，不能作为可靠事实；如需公式问答请单独评估公式增强",
                    source=_first_source(node.get("prov"), fmt, section_path, sheet_name),
                    detail=f"node={child_ref}",
                ))
            if label == "page_header" or label == "page_footer":
                warnings.append(QualityWarning(
                    code="furniture_excluded", severity="info", scope="block",
                    message="页眉页脚已保留在预览中，但不参与检索分块，避免污染正文检索",
                    source=_first_source(node.get("prov"), fmt, section_path, sheet_name),
                    detail=f"node={child_ref}",
                ))
            block = Block(
                id=f"{document_id}-b{order}", document_id=document_id, parse_version_id="",
                order_index=order, block_type=block_type, label=label, text=text,
                heading_path=section_path, sheet_name=sheet_name,
                sources=_prov_to_sources(node.get("prov"), fmt, section_path, sheet_name),
                char_count=len(text),
            )
            blocks.append(block)
            order += 1
            # Docling 会把正文、表格与图片挂在标题节点的 children 下（DOCX 常见），
            # 只遍历 body.children 会丢掉标题以下的全部内容，因此这里把子节点按顺序入队。
            for nested in reversed(node.get("children") or []):
                nested_ref = ref_key(nested)
                if nested_ref and nested_ref not in table_child_refs:
                    queue.insert(0, nested_ref)
        elif child_ref in tables:
            table = tables[child_ref]
            label = str(table.get("label") or "table")
            block_type = _TABLE_LABEL_TYPE.get(label)
            if block_type is None:
                warnings.append(QualityWarning(
                    code="unknown_table_label", severity="warning", scope="table",
                    message=f"表格使用了未识别的标签「{label}」，已按表格处理并提示人工核对",
                    detail=f"node={child_ref}",
                ))
                block_type = "table"
            table_no += 1
            sheet_name = current_sheet(table)
            section_path = _extract_heading_path(table, texts)
            table_dict = _table_to_dict(table)
            table_dict["table_no"] = table_no
            table_dict["label"] = label
            # Docling 的标题/脚注通常是引用，必须解引用后保存，不能只接受内联 text。
            for field in ("captions", "footnotes"):
                values = []
                for item in table.get(field) or []:
                    resolved = texts.get(ref_key(item), item) if isinstance(item, dict) else {}
                    if isinstance(resolved.get("text"), str):
                        values.append(resolved["text"])
                table_dict[field] = values
            raw_text = _table_plain_text(table)
            sources = _prov_to_sources(table.get("prov"), fmt, section_path, sheet_name)
            if block_type == "document_index":
                # 目录被识别成表格：点线引导符会污染检索文本，只在目录上下文清理。
                cleaned = _clean_index_text(raw_text)
                warnings.append(QualityWarning(
                    code="toc_dot_leaders", severity="info", scope="table",
                    page=sources[0].page if sources else None,
                    message="该表格被识别为目录（点线引导），已按目录上下文保守清理引导符，"
                            "页码对应关系仍需对照原件核对",
                    source=sources[0] if sources else None,
                    detail=f"node={child_ref}",
                ))
                raw_text = cleaned
            if fmt == "xlsx":
                _annotate_xlsx_table(table_dict, sources, sheet_name, warnings)
            block = Block(
                id=f"{document_id}-b{order}", document_id=document_id, parse_version_id="",
                order_index=order, block_type=block_type, label=label,
                text=raw_text, heading_path=section_path, table=table_dict,
                sheet_name=sheet_name, sources=sources, char_count=len(raw_text),
            )
            blocks.append(block)
            order += 1
        elif child_ref in pictures:
            picture = pictures[child_ref]
            section_path = _extract_heading_path(picture, texts)
            sheet_name = current_sheet(picture)
            sources = _prov_to_sources(picture.get("prov"), fmt, section_path, sheet_name)
            # 图片与统计图不做语义解析：只保留位置，明确告警，不把图内文字当可靠知识。
            warnings.append(QualityWarning(
                code="picture_not_semantically_parsed", severity="info", scope="block",
                message="图片/图表未做语义解析，图内文字与数据系列不作为可靠事实",
                source=sources[0] if sources else None,
                detail=f"node={child_ref}",
            ))
            blocks.append(Block(
                id=f"{document_id}-b{order}", document_id=document_id, parse_version_id="",
                order_index=order, block_type="picture", label=str(picture.get("label") or "picture"),
                text="", heading_path=section_path, sheet_name=sheet_name,
                sources=sources, char_count=0,
            ))
            order += 1
        elif child_ref in groups:
            group = groups[child_ref]
            # 分组本身不产生内容：递归其子节点以保持阅读顺序。
            # XLSX 的 sheet 分组在这里递归，使表格与文本块都能继承工作表名；
            # PDF 的 list 分组同样递归，避免丢失列表项。
            queue[0:0] = [ref_key(nested) for nested in (group.get("children") or [])]
        else:
            warnings.append(QualityWarning(
                code="unresolved_node_reference", severity="warning", scope="document",
                message="存在无法解析的结构引用，可能缺少部分内容，请对照原件核对",
                detail=f"ref={child_ref}",
            ))

        # 为每个结构块补齐节点引用，Office 没有 prov 时也能追溯到章节/表格。
        if blocks and child_ref not in groups:
            for source in blocks[-1].sources:
                if source.node_ref is None:
                    source.node_ref = child_ref
                if blocks[-1].table:
                    source.table_no = blocks[-1].table.get('table_no')

    document.blocks = blocks
    _add_document_level_warnings(document, fmt, pages, texts, tables, pictures, warnings)
    document.stats = {
        "block_count": len(blocks),
        "text_nodes": len(texts),
        "table_nodes": len(tables),
        "picture_nodes": len(pictures),
        "table_cell_text_nodes_skipped": len(table_child_refs),
        "text_length": document.text_length(),
    }
    return document


def _first_source(prov: Any, fmt: str, section_path: str | None,
                  sheet_name: str | None) -> SourceLocation | None:
    sources = _prov_to_sources(prov, fmt, section_path, sheet_name)
    return sources[0] if sources else None


def _add_document_level_warnings(document: NormalizedDocument, fmt: str, pages: dict,
                                 texts: dict, tables: dict, pictures: dict,
                                 warnings: list[QualityWarning]) -> None:
    """文档级告警：空白页、无可索引内容、扫描件 OCR 局限、图表未解析统计。"""
    if fmt == "pdf" and pages:
        used_pages: set[int] = set()
        for node in list(texts.values()) + list(tables.values()) + list(pictures.values()):
            for item in node.get("prov") or []:
                page_no = item.get("page_no") if isinstance(item, dict) else None
                if isinstance(page_no, int):
                    used_pages.add(page_no)
        blank_pages = [int(key) for key in pages.keys() if int(key) not in used_pages]
        for page_no in sorted(blank_pages):
            # 空白页继续占原页号：这里只记录告警，不删除页、不位移后续页码。
            warnings.append(QualityWarning(
                code="blank_page", severity="info", scope="page", page=page_no,
                message=f"第 {page_no} 页没有可提取内容（可能是合法空白页），页码保持原编号不变",
            ))
    indexable = document.indexable_blocks()
    if not indexable:
        warnings.append(QualityWarning(
            code="no_indexable_content", severity="error", scope="document",
            message="整份文档没有可索引内容，不会建立“成功”索引；请确认原件是否为空或解析是否失败",
        ))
    if fmt == "pdf" and document.blocks:
        # 扫描/混合 PDF 的 OCR 局限：只做提示，不声称能自动发现所有错字或阅读顺序错误。
        warnings.append(QualityWarning(
            code="ocr_limitation", severity="info", scope="document",
            message="扫描或混合 PDF 的识别结果可能存在错字、目录页码错位与阅读顺序问题，"
                    "请结合原件核对；系统不声称能自动发现全部 OCR 错误",
        ))
    picture_count = len(pictures)
    if picture_count:
        warnings.append(QualityWarning(
            code="pictures_not_parsed", severity="info", scope="document",
            message=f"文档含 {picture_count} 个图片/图表节点，未做语义解析，其内部文字与数据系列不作为可靠事实",
        ))


def _column_letters(index: int) -> str:
    """零基列序号转 Excel 列名（0→A、25→Z、26→AA）。"""
    letters = ""
    current = index
    while True:
        letters = chr(ord("A") + current % 26) + letters
        current = current // 26 - 1
        if current < 0:
            return letters


def _annotate_xlsx_table(table_dict: dict[str, Any], sources: list[SourceLocation],
                         sheet_name: str | None, warnings: list[QualityWarning]) -> None:
    """把 XLSX 表来源网格起点与表内偏移组合成真实单元格坐标。

    实测样本中表来源 bbox 为 l=0,t=1,r=6,b=8（零基列、一基行、右/下边界为开区间），
    结合表内 0 基偏移可还原真实单元格；不能把表内第一行当工作表第一行，
    也不能把这类 bbox 当作 PDF 点坐标。
    """
    if not sources:
        warnings.append(QualityWarning(
            code="xlsx_source_missing", severity="warning", scope="table",
            message="XLSX 表格缺少来源网格信息，无法定位真实单元格范围",
        ))
        return
    source = sources[0]
    bbox = source.bbox or {}
    left = bbox.get("l")
    top = bbox.get("t")
    right = bbox.get("r")
    bottom = bbox.get("b")
    if not all(isinstance(value, (int, float)) for value in (left, top, right, bottom)):
        warnings.append(QualityWarning(
            code="xlsx_source_missing", severity="warning", scope="table",
            message="XLSX 表格来源缺少有效网格坐标，无法定位真实单元格范围",
        ))
        return
    base_col = int(left)
    base_row = int(top)  # 一基行号：t=1 表示工作表第 2 行。
    num_rows = table_dict.get("num_rows") or 0
    num_cols = table_dict.get("num_cols") or 0
    end_row = int(bottom)  # 右/下边界为开区间，b=8 表示到第 8 行结束。
    end_col = int(right) - 1
    cell_range = f"{_column_letters(base_col)}{base_row + 1}:{_column_letters(end_col)}{end_row}"
    source.sheet_name = sheet_name
    source.cell_range = cell_range
    source.note = (f"工作表网格起点（零基列 {base_col}、一基行 {base_row + 1}）+ 表内偏移还原；"
                   "bbox 为工作表网格坐标，不是 PDF 点坐标")
    for cell in table_dict.get("cells", []):
        row_offset = cell.get("row")
        col_offset = cell.get("col")
        if not isinstance(row_offset, int) or not isinstance(col_offset, int):
            continue
        cell_row = base_row + row_offset + 1
        cell_col = base_col + col_offset
        cell["cell"] = f"{_column_letters(cell_col)}{cell_row}"
    table_dict["cell_range"] = cell_range
    table_dict["sheet_name"] = sheet_name
    if num_rows and num_cols and (end_row - base_row) != num_rows:
        warnings.append(QualityWarning(
            code="xlsx_range_mismatch", severity="warning", scope="table",
            message=f"XLSX 表格网格范围（{cell_range}）与表内行数 {num_rows} 不一致，请对照原件核对",
        ))


# ---------------------------------------------------------------------------
# XLSX 公式与缓存检查（只读 openpyxl，不修改原件、不重算）
# ---------------------------------------------------------------------------
def inspect_xlsx_formulas(path, *, sheet_filter: set[str] | None = None) -> dict[str, Any]:
    """只读检查工作簿中的公式表达式与文件保存的缓存值。

    返回 {"formulas": {工作表: {单元格: {...}}}, "merged": {工作表: [范围]}}。
    只读取 data_only=True 的缓存值与 data_only=False 的公式，不执行宏、外链或重算。
    """
    result: dict[str, Any] = {"formulas": {}, "merged": {}, "sheets": [], "error": None}
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover - 依赖已在运行依赖中声明
        result["error"] = "服务端缺少 openpyxl，无法检查公式缓存"
        return result
    try:
        # 业务原件以无后缀的服务端 ID 保存，openpyxl 只按扩展名判断格式会抛
        # InvalidFileException；这里把内容读入内存并用 BytesIO 提供，
        # 既明确格式又避免 read_only 惰性读取期间文件句柄被关闭（seek of closed file）。
        raw = Path(path).read_bytes()
        # data_only=True 读取文件保存的缓存值；公式表达式需要另开一次读取。
        cached = load_workbook(filename=BytesIO(raw), data_only=True, read_only=True)
        formulas = load_workbook(filename=BytesIO(raw), data_only=False, read_only=True)
    except Exception as exc:  # noqa: BLE001 - 损坏或加密工作簿转为可读提示
        result["error"] = f"无法读取工作簿以检查公式：{type(exc).__name__}"
        return result
    try:
        result["sheets"] = list(formulas.sheetnames)
        for name in formulas.sheetnames:
            if sheet_filter and name not in sheet_filter:
                continue
            formula_sheet: dict[str, Any] = {}
            cached_sheet = cached[name] if name in cached.sheetnames else None
            for row in formulas[name].iter_rows():
                for cell in row:
                    value = cell.value
                    if not isinstance(value, str) or not value.startswith("="):
                        continue
                    cached_value = None
                    if cached_sheet is not None:
                        try:
                            cached_value = cached_sheet[cell.coordinate].value
                        except (KeyError, ValueError):
                            cached_value = None
                    formula_sheet[cell.coordinate] = {
                        "formula": value,
                        # 缓存为空表示文件保存的缓存缺失，不能当作 0 或重新计算结果。
                        "cached_value": cached_value,
                        "has_cache": cached_value is not None and str(cached_value) != "",
                    }
            if formula_sheet:
                result["formulas"][name] = formula_sheet
            # read_only 模式下 merged_cells 可能是列表而不是带 ranges 的对象，两种都要兼容。
            merged_cells = getattr(formulas[name], "merged_cells", None)
            if merged_cells is None:
                merged = []
            elif hasattr(merged_cells, "ranges"):
                merged = [str(item) for item in merged_cells.ranges]
            else:
                merged = [str(item) for item in merged_cells]
            if merged:
                result["merged"][name] = merged
    finally:
        cached.close()
        formulas.close()
    return result


def apply_formula_warnings(document: NormalizedDocument, formula_info: dict[str, Any]) -> None:
    """根据公式检查结果生成告警，并补充公式表达式到对应表格文本。

    规则：
    - 缓存为空 → formula_cache_missing 告警，不得当 0，也不得交由模型猜结果；
    - 有缓存也说明是“文件保存的缓存”，不宣称已重新计算或一定最新；
    - 公式表达式与缓存状态写入表格结构，便于预览与分块携带。
    """
    if formula_info.get("error"):
        document.warnings.append(QualityWarning(
            code="xlsx_formula_inspection_failed", severity="warning", scope="document",
            message="无法读取工作簿公式信息，公式结果是否可用未验证",
            detail=str(formula_info["error"]),
        ))
        return
    formulas: dict[str, dict[str, Any]] = formula_info.get("formulas") or {}
    if not formulas:
        return
    total = sum(len(items) for items in formulas.values())
    missing: list[str] = []
    for sheet_name, cells in formulas.items():
        for coordinate, info in sorted(cells.items()):
            if not info.get("has_cache"):
                missing.append(f"{sheet_name}!{coordinate}")
    for block in document.blocks:
        if block.block_type not in {"table", "document_index"} or not block.table:
            continue
        sheet = block.sheet_name
        if sheet not in formulas:
            continue
        table_formulas = {coordinate: info for coordinate, info in formulas[sheet].items()}
        block.table["formulas"] = table_formulas
        # 把公式表达式追加到表格文本，避免表达式在检索文本中丢失。
        lines = [block.text]
        for coordinate, info in sorted(table_formulas.items()):
            cache = info.get("cached_value")
            cache_text = "" if cache is None else str(cache)
            lines.append(f"{coordinate} = {info['formula']}（文件保存的缓存值："
                         f"{cache_text if info.get('has_cache') else '缺失'}）")
        block.text = "\n".join(line for line in lines if line)
        block.char_count = len(block.text)
    if missing:
        document.warnings.append(QualityWarning(
            code="formula_cache_missing", severity="warning", scope="document",
            message=f"工作簿共有 {total} 个公式单元格，其中 {len(missing)} 个没有保存的缓存值，"
                    "这些位置不能当作 0 或空白；本期不启动 Excel/LibreOffice 自动重算",
            detail="、".join(missing[:40]) + ("…" if len(missing) > 40 else ""),
        ))
    else:
        document.warnings.append(QualityWarning(
            code="formula_cache_present", severity="info", scope="document",
            message=f"工作簿共有 {total} 个公式单元格，均带文件保存的缓存值；"
                    "缓存来自文件保存时，未重新计算，不保证是最新结果",
        ))


def merge_warnings(warnings: list[QualityWarning]) -> list[QualityWarning]:
    """合并同码同范围的重复告警，避免同一问题在页面重复显示。"""
    seen: dict[tuple, QualityWarning] = {}
    for warning in warnings:
        key = (warning.code, warning.block_id, warning.page, warning.sheet_name, warning.detail)
        if key not in seen:
            seen[key] = warning
    return list(seen.values())


def overall_quality_status(warnings: list[QualityWarning]) -> str:
    """由告警计算质量状态：error 视为结构无效，任何告警都视为“有质量告警”。

    提示级（info）告警同样计入 warnings：空白页、页眉页脚排除、OCR 局限、
    图表未语义解析等都会影响内容可信度判断，不能让页面显示为“无质量问题”。
    """
    if any(warning.severity == "error" for warning in warnings):
        return "invalid"
    if warnings:
        return "warnings"
    return "ok"
