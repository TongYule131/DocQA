# 真实 Docling 结果适配器测试（离线夹具）。
#
# 夹具来自 2026-09-23 的真实 GPU 服务转换结果（data/docling-validation/...-gpu-seven-acceptance/），
# 精简压缩副本随 tests/fixtures/docling 提交。这些测试验证的是“真实上游结构能否被本项目的规范化与分块正确处理”，
# 不重新调用解析服务，也不替代 12.1 要求的真实网页上传链路。
#
# 夹具缺失属于交付错误，直接失败，不静默跳过。
import json
import gzip
from pathlib import Path

import pytest

from app.chunking import ChunkingConfig, chunk_document
from app.document_normalizer import normalize_docling_result

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "docling"

# 样本 -> (格式, 结构化结果文件名)
SAMPLES = {
    "01": ("pdf", "01-text-en-single-column.structured.json"),
    "02": ("pdf", "02-text-en-two-column-tables.structured.json"),
    "03": ("pdf", "03-text-zh-tables.structured.json"),
    "04": ("pdf", "04-scan-zh.structured.json"),
    "05": ("pdf", "05-mixed-text-scan.structured.json"),
    "06": ("docx", "06-sample.structured.json"),
    "07": ("xlsx", "07-sample.structured.json"),
}


def load(sample: str) -> dict:
    fmt, name = SAMPLES[sample]
    path = FIXTURE_ROOT / (name + '.gz')
    return json.loads(gzip.decompress(path.read_bytes()).decode('utf-8'))



def normalize(sample: str):
    fmt, _ = SAMPLES[sample]
    return normalize_docling_result(f"real-{sample}", load(sample), fmt)


def test_real_pdf_sources_keep_page_and_bbox():
    """真实 PDF 结果的来源必须带一基页号、原始 bbox、坐标原点与单位。"""
    normalized = normalize("01")
    pdf_sources = [source for block in normalized.blocks for source in block.sources
                   if source.format == "pdf"]
    assert pdf_sources, "真实 PDF 结果必须产生 PDF 来源"
    for source in pdf_sources[:50]:
        assert isinstance(source.page, int) and source.page >= 1
        assert source.bbox and source.coord_origin in {"TOPLEFT", "BOTTOMLEFT"}
        assert source.coord_unit == "pt"
    # 页码不超出上游 pages 清单。
    page_numbers = {int(key) for key in load("01")["pages"]}
    assert {source.page for source in pdf_sources} <= page_numbers
    # 公式节点内容为空时必须有明确告警，不得填补内容。
    assert any(warning.code == "formula_content_missing" for warning in normalized.warnings)


def test_real_pdf_blank_page_is_kept_with_numbering():
    """03 样本第 12 页为真实空白页：保留页号并给出提示，不导致整份失败。"""
    normalized = normalize("03")
    blank = [warning for warning in normalized.warnings if warning.code == "blank_page"]
    assert [warning.page for warning in blank] == [12]
    # 空白页不产生块，但后续页码不位移。
    pages = {source.page for block in normalized.blocks for source in block.sources if source.page}
    assert 13 in pages and 12 not in pages


def test_real_document_index_tables_get_page_and_clean_text():
    """目录被识别为表格：必须有页码来源，且点线引导符被保守清理。"""
    normalized = normalize("03")
    index_blocks = [block for block in normalized.blocks if block.block_type == "document_index"]
    assert len(index_blocks) >= 7, "真实样本含 7 个目录表"
    assert all(block.sources and block.sources[0].page for block in index_blocks)
    warning_pages = {warning.page for warning in normalized.warnings
                     if warning.code == "toc_dot_leaders" and warning.page}
    # 目录表分布在第 5—11 页；文本块中的独立点线也按所在页记录。
    assert {5, 6, 7, 8, 9, 10, 11} <= warning_pages
    # 清理只针对目录上下文：正文中的小数与百分号必须保留。
    text = "\n".join(block.text for block in normalized.blocks)
    assert "1.9" in text or "2.0" in text or "1.2" in text or "1.5" in text
    # 目录块文本中的连续点线应显著减少（不等于 0 也允许，因为页码后的点号可能保留）。
    assert "......" not in text


def test_real_scan_pdf_has_six_pages_and_warnings():
    """04 扫描件：6 页都有来源，并明确给出 OCR 局限提示。"""
    normalized = normalize("04")
    assert normalized.page_count == 6
    pages = {source.page for block in normalized.blocks for source in block.sources if source.page}
    assert pages == {1, 2, 3, 4, 5, 6}
    codes = {warning.code for warning in normalized.warnings}
    assert "ocr_limitation" in codes
    # 页眉页脚保留在预览中但不参与检索分块。
    assert "furniture_excluded" in codes or not any(
        block.block_type in {"page_header", "page_footer"} for block in normalized.blocks)
    chunks = chunk_document(normalized, ChunkingConfig())
    assert chunks
    assert all(chunk.text.strip() for chunk in chunks)


def test_real_docx_uses_section_path_without_fake_pages():
    """06 DOCX：章节路径来自标题父子结构，表格无真实页码，合并信息保留。"""
    normalized = normalize("06")
    assert normalized.page_count == 0
    # 标题以下的内容通过 children 挂在标题节点上，必须全部按阅读顺序取出。
    block_types = [block.block_type for block in normalized.blocks]
    assert block_types.count("section_header") == 3, f"实际块类型：{block_types}"
    assert block_types.count("paragraph") >= 4
    sections = [block.text for block in normalized.blocks if block.block_type == "section_header"]
    assert "一、总体情况" in sections and "三、结论" in sections
    # 每个章节标题都携带顶层标题作为章节路径。
    paths = {block.heading_path for block in normalized.blocks if block.block_type == "section_header"}
    assert "2024 年度经营分析报告" in paths
    for block in normalized.blocks:
        assert block.sources, "DOCX 即使没有页坐标，也必须保留章节与节点来源"
        for source in block.sources:
            assert source.format == "docx"
            assert source.page is None, "DOCX 不得伪造页码"
            assert source.node_ref
    table_block = next(block for block in normalized.blocks if block.block_type == "table")
    # 样本表格为 8 行 × 5 列，标题列跨 5 列，存在跨行跨列合并。
    assert table_block.table["num_cols"] == 5
    spans = {(cell["col_span"], cell["row_span"]) for cell in table_block.table["cells"]}
    assert (5, 1) in spans and (1, 2) in spans
    # 表格单元格文本不得被当成独立正文块重复索引。
    body_texts = [block.text.strip() for block in normalized.blocks
                  if block.block_type == "paragraph"]
    assert "区域" not in body_texts
    chunks = chunk_document(normalized, ChunkingConfig())
    assert all(chunk.page is None for chunk in chunks)
    assert any("表头：" in chunk.text for chunk in chunks if chunk.chunk_type == "table_rows")


def test_real_xlsx_sheet_names_and_real_cell_ranges():
    """07 XLSX：3 个工作表名与真实单元格范围，第一张表从 A2 开始（不是 A1）。"""
    normalized = normalize("07")
    tables = [block for block in normalized.blocks if block.block_type == "table"]
    assert [block.sheet_name for block in tables] == ["分区域数据", "指标说明", "原始记录"]
    ranges = [block.sources[0].cell_range for block in tables]
    assert ranges[0] == "A2:F8"
    assert ranges[1] == "A1:C6" and ranges[2] == "A1:D5"
    for block in tables:
        assert block.sources[0].format == "xlsx"
        assert block.sources[0].page is None  # 网格页不是物理 PDF 页码，用工作表名定位。
        assert "工作表网格坐标" in (block.sources[0].note or "")
    # 单元格坐标必须结合网格起点与表内偏移，而不是表内行号加一。
    first = tables[0]
    coordinates = {cell.get("cell") for cell in first.table["cells"]}
    assert "A2" in coordinates and "F8" in coordinates
    # 分块携带工作表名与范围，Markdown 不保留的工作表标签不会丢失。
    chunks = chunk_document(normalized, ChunkingConfig())
    assert any("工作表：分区域数据" in chunk.text and "A2:F8" in chunk.text for chunk in chunks)


def test_real_xlsx_formula_cache_missing_warning():
    """07 XLSX 的 B8:F8 公式无缓存：必须告警且不得变成 0。"""
    import openpyxl
    source = FIXTURE_ROOT / "07-sample.xlsx"
    from app.document_normalizer import apply_formula_warnings, inspect_xlsx_formulas

    normalized = normalize("07")
    info = inspect_xlsx_formulas(source)
    assert info["error"] is None
    assert len(info["formulas"]["分区域数据"]) == 5
    assert all(not item["has_cache"] for item in info["formulas"]["分区域数据"].values())
    apply_formula_warnings(normalized, info)
    missing = [warning for warning in normalized.warnings if warning.code == "formula_cache_missing"]
    assert missing and "分区域数据!B8" in (missing[0].detail or "")
    chunks = chunk_document(normalized, ChunkingConfig())
    joined = "\n".join(chunk.text for chunk in chunks)
    assert "=SUM" in joined
    assert "缺失" in joined
    # 原件未被修改：再次检查仍然没有缓存值。
    assert all(not item["has_cache"]
               for item in inspect_xlsx_formulas(source)["formulas"]["分区域数据"].values())
