"""M2 U03: new-version structure recovery against synthetic positive and negative cases.

No source documents are rewritten and no provider, parsing service, or model is used.
Existing real Docling fixture tests separately cover adapter compatibility.
"""
from copy import deepcopy

import pytest

from app.chunking import CHUNKING_ALGO, ChunkingConfig, chunk_document
from app.document_normalizer import RESULT_SCHEMA_VERSION, NormalizedDocument, normalize_docling_result
from app.schemas import Block, SourceLocation


def payload(entries):
    """Keep body order explicit; table markers occupy their original position."""
    texts, tables, children = [], [], []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"text": entry}
        page = entry.get("page", 1)
        common = {"parent": {"$ref": "#/body"}, "prov": [{"page_no": page,
            "bbox": {"l": 10, "t": 20, "r": 200, "b": 40, "coord_origin": "TOPLEFT"},
            "charspan": [0, len(entry.get("text", ""))]}]}
        if entry.get("label") == "table":
            ref = f"#/tables/{len(tables)}"
            cells = [{"text": "指标", "start_row_offset_idx": 0, "start_col_offset_idx": 0,
                      "row_span": 1, "col_span": 1, "column_header": True},
                     {"text": "95.3%", "start_row_offset_idx": 1, "start_col_offset_idx": 0,
                      "row_span": 1, "col_span": 1}]
            tables.append({**common, "self_ref": ref, "label": "table", "children": [],
                "data": {"num_rows": 2, "num_cols": 1, "table_cells": cells}})
        else:
            ref = f"#/texts/{len(texts)}"
            texts.append({**common, "self_ref": ref, "label": "text", **entry})
        children.append({"$ref": ref})
    return {"body": {"self_ref": "#/body", "children": children}, "texts": texts,
            "tables": tables, "groups": [], "pictures": [],
            "pages": {str(page): {"page_no": page} for page in range(1, 5)}}


def heading(text, level=1, **extra):
    return {"label": "section_header", "text": text, "level": level, **extra}


def normalize(entries, fmt="pdf"):
    return normalize_docling_result("synthetic-structure", payload(entries), fmt)


def by_text(document, text):
    return next(block for block in document.blocks if block.text == text)


def test_flat_chinese_numbering_restores_multilevel_paths_and_table_section():
    document = normalize([
        {"label": "title", "text": "教学设计研究报告"}, heading("一、总体设计"), "总述正文",
        heading("（一）认知目标"), "认知说明", heading("1. 基础任务"), "基础任务正文",
        heading("（1）输入判断"), "输入判断正文", heading("2. 迁移任务"), "迁移任务正文",
        heading("二、实践效果"), {"label": "table"},
    ])
    root = "教学设计研究报告 / 一、总体设计 / （一）认知目标"
    assert by_text(document, "基础任务正文").heading_path == f"{root} / 1. 基础任务"
    assert by_text(document, "输入判断正文").heading_path == f"{root} / 1. 基础任务 / （1）输入判断"
    assert by_text(document, "迁移任务正文").heading_path == f"{root} / 2. 迁移任务"
    table = next(block for block in document.blocks if block.block_type == "table")
    assert table.heading_path == "教学设计研究报告 / 二、实践效果"
    assert all(source.section_path == table.heading_path for source in table.sources)
    table_chunks = [chunk for chunk in chunk_document(document, ChunkingConfig()) if chunk.chunk_type == "table_rows"]
    assert table_chunks and all(chunk.heading_path == table.heading_path for chunk in table_chunks)
    assert all(chunk.block_id == table.id for chunk in table_chunks)


def test_arabic_numbering_overrides_flat_docling_levels_without_inventing_missing_parent():
    document = normalize([heading("1 Introduction"), heading("1.1 Design"), "设计正文",
                          heading("1.1.1 Scope"), "范围正文", heading("1.2 Validation"), "验证正文",
                          heading("2.1 Missing parent"), "无父标题的正文"])
    assert by_text(document, "范围正文").heading_path == "1 Introduction / 1.1 Design / 1.1.1 Scope"
    assert by_text(document, "验证正文").heading_path == "1 Introduction / 1.2 Validation"
    assert by_text(document, "无父标题的正文").heading_path == "2.1 Missing parent"


def test_chapter_section_numbering_and_unnumbered_docling_levels():
    document = normalize([heading("第一章 总体方案"), heading("第一节 技术背景"), "背景正文",
                          heading("第二节 实施路径"), "路径正文", heading("第二章 评价方法"), "评价正文"])
    assert by_text(document, "背景正文").heading_path == "第一章 总体方案 / 第一节 技术背景"
    assert by_text(document, "路径正文").heading_path == "第一章 总体方案 / 第二节 实施路径"
    assert by_text(document, "评价正文").heading_path == "第二章 评价方法"
    raw = payload([heading("背景说明", 1), heading("核心内容", 2), heading("关键步骤", 3), "步骤正文",
                   heading("实践方案", 1), "方案正文"])
    # Even PDF nodes without prov retain their Docling level for hierarchy recovery.
    for node in raw["texts"]:
        node["prov"] = []
    document = normalize_docling_result("no-prov", raw, "pdf")
    assert by_text(document, "步骤正文").heading_path == "背景说明 / 核心内容 / 关键步骤"
    assert by_text(document, "方案正文").heading_path == "实践方案"


def test_mixed_part_chapter_and_chinese_numbering_keeps_each_parent_scope():
    document = normalize([heading("第一篇 课程设计"), heading("第一章 总体方案"),
                          heading("第一节 核心目标"), heading("一、认知目标"), "目标正文",
                          heading("二、任务安排"), "任务正文", heading("第二章 课堂实施"), "课堂正文",
                          heading("第二篇 教学评价"), "评价正文"])
    root = "第一篇 课程设计 / 第一章 总体方案 / 第一节 核心目标"
    assert by_text(document, "目标正文").heading_path == f"{root} / 一、认知目标"
    assert by_text(document, "任务正文").heading_path == f"{root} / 二、任务安排"
    assert by_text(document, "课堂正文").heading_path == "第一篇 课程设计 / 第二章 课堂实施"
    assert by_text(document, "评价正文").heading_path == "第二篇 教学评价"


def test_cross_page_continuation_preserves_ocr_spacing_and_all_source_fields():
    original = "  认 知 结 构并未在此处改写，\n续段仍保留原始换行与  空格。  "
    raw = payload([heading("一、研究方法"), {"text": original, "page": 1},
                   {"label": "page_header", "text": "页眉标题", "page": 2},
                   {"text": "跨页续写的原文。", "page": 2}])
    snapshot = deepcopy(raw)
    document = normalize_docling_result("cross-page", raw, "pdf")
    assert raw == snapshot
    block = by_text(document, original)
    assert block.text == original and block.char_count == len(original)
    assert block.sources[0].bbox == snapshot["texts"][1]["prov"][0]["bbox"]
    assert (block.sources[0].char_start, block.sources[0].char_end) == (0, len(original))
    assert by_text(document, "跨页续写的原文。").heading_path == "一、研究方法"
    chunks = chunk_document(document, ChunkingConfig())
    assert not any("页眉标题" in chunk.text for chunk in chunks)
    assert any("跨页续写的原文。" in chunk.text for chunk in chunks)


@pytest.mark.parametrize("bad", ["", "  ", "李晓梅", "李 晓 梅", "李 晓梅", "张三", "欧阳若兰", "张三、李四", "2024", "2024 年",
    "2024年10月", "[1] 张三. 教育研究, 2024.", "［12］ 引用条目", "1. 张三，教育研究，2024",
    "1.5亿元", "25%", "3倍", "12人", "本文通过案例进行分析", "素养目标的", "因此需要进一步验证",
    "1.2", "（一）", "一、", "这是一个完整句子。"])
def test_false_section_labels_do_not_create_boundaries_or_change_original_text(bad):
    document = normalize([heading("一、教学方法"), "前文", heading(bad), "后文"])
    suspect = document.blocks[2]
    assert suspect.text == bad
    assert suspect.label == "section_header" and suspect.block_type == "paragraph"
    assert by_text(document, "后文").heading_path == "一、教学方法"
    assert document.stats["structure"]["suppressed_heading_order_indices"] == [suspect.order_index]
    assert document.stats["structure"]["suppressed_heading_node_refs"] == ["#/texts/2"]
    assert any(warning.code == "heading_labels_unreliable" for warning in document.warnings)


def test_body_numbering_and_reference_items_are_not_promoted_to_headings():
    document = normalize([heading("一、实践观察"), "1. 学生观察训练结果。", "2024 年完成了三次测试。",
                          {"label": "list_item", "text": "（一）这是正文列表而不是标题"},
                          {"label": "reference", "text": "[1] 某研究, 2024."}, "尾部正文"])
    assert [block.block_type for block in document.blocks] == ["section_header", "paragraph", "paragraph",
                                                            "list_item", "reference", "paragraph"]
    assert all(block.heading_path == "一、实践观察" for block in document.blocks)


def test_bibliography_mislabels_stay_in_references_until_explicit_appendix():
    document = normalize([heading("一、研究结论"), "结论正文", heading("参考文献"),
                          heading("1. 某研究文献"), heading("某作者研究概述"), "文献续行",
                          heading("附录 A 实验设置"), "附录正文"])
    assert by_text(document, "1. 某研究文献").block_type == "paragraph"
    assert by_text(document, "某作者研究概述").block_type == "paragraph"
    assert by_text(document, "文献续行").heading_path == "参考文献"
    assert by_text(document, "附录正文").heading_path == "附录 A 实验设置"


def test_explicit_parent_paths_keep_their_original_values_and_cross_groups():
    raw = payload([{"label": "title", "text": "旧版报告"}, heading("一、原有章节"),
                   heading("1.1 原有子节"), "有显式父链的正文", {"label": "table"}, "缺失父链的续写"])
    raw["texts"][1]["parent"] = {"$ref": "#/texts/0"}
    raw["texts"][2]["parent"] = {"$ref": "#/texts/1"}
    raw["texts"][3]["parent"] = {"$ref": "#/groups/0"}
    raw["tables"][0]["parent"] = {"$ref": "#/groups/0"}
    raw["groups"] = [{"self_ref": "#/groups/0", "label": "list", "parent": {"$ref": "#/texts/2"},
                      "children": [{"$ref": "#/texts/3"}, {"$ref": "#/tables/0"}]}]
    raw["texts"][2]["children"] = [{"$ref": "#/groups/0"}]
    document = normalize_docling_result("explicit", raw, "docx")
    assert by_text(document, "一、原有章节").heading_path == "旧版报告"
    assert by_text(document, "1.1 原有子节").heading_path == "旧版报告 / 一、原有章节"
    path = "旧版报告 / 一、原有章节 / 1.1 原有子节"
    assert by_text(document, "有显式父链的正文").heading_path == path
    assert by_text(document, "缺失父链的续写").heading_path == path
    assert next(block for block in document.blocks if block.table).heading_path == path
    assert all(source.page is None for block in document.blocks for source in block.sources)


def test_caption_context_associations_do_not_rewrite_quotes_or_cross_sections():
    document = normalize([heading("一、模型结构"), "正文先解释外环和内环。",
                          {"label": "caption", "text": "图 1 实践模型"}, "图后的说明正文。",
                          heading("二、评价方法"), {"label": "caption", "text": "图 2 评价结构"}])
    caption = by_text(document, "图 1 实践模型")
    contexts = document.stats["structure"]["caption_context_order_indices"]
    assert contexts[str(caption.order_index)] == [by_text(document, "正文先解释外环和内环。").order_index,
                                                 by_text(document, "图后的说明正文。").order_index]
    assert str(by_text(document, "图 2 评价结构").order_index) not in contexts
    assert document.stats["structure"]["caption_context_node_refs"] == {"#/texts/2": ["#/texts/1", "#/texts/3"]}
    assert caption.text == "图 1 实践模型"
    for chunk in chunk_document(document, ChunkingConfig()):
        if "图 1" in chunk.text:
            assert chunk.block_id in {by_text(document, "正文先解释外环和内环。").id, caption.id}
            assert {source.node_ref for source in chunk.sources} >= {"#/texts/1", "#/texts/2", "#/texts/3"}
            assert "一、模型结构" not in chunk.text  # hierarchy is metadata, not original quoted prose


def test_large_duplicate_reference_sequence_keeps_nested_and_trailing_content():
    raw = payload([heading("一、开始章节"), "必须保留的中段", heading("二、尾部章节"), "必须保留的末段"])
    raw["groups"] = [{"self_ref": "#/groups/0", "label": "list", "children":
                      [{"$ref": "#/texts/0"}] * 5000 + [{"$ref": "#/texts/1"}, {"$ref": "#/groups/0"}]}]
    raw["body"]["children"] = [{"$ref": "#/groups/0"}] * 5000 + [
        {"$ref": "#/texts/2"}, {"$ref": "#/texts/3"}]
    document = normalize_docling_result("tail", raw, "pdf")
    assert [block.text for block in document.blocks] == ["一、开始章节", "必须保留的中段", "二、尾部章节", "必须保留的末段"]
    assert [block.order_index for block in document.blocks] == list(range(4))
    assert by_text(document, "必须保留的末段").heading_path == "二、尾部章节"
    chunks = chunk_document(document, ChunkingConfig(max_chars=30, overlap_chars=5))
    assert chunks[-1].text == "必须保留的末段"


def test_old_normalized_blocks_are_not_recovered_or_rewritten_when_only_chunked():
    blocks = [Block(id="legacy-b0", document_id="legacy", parse_version_id="legacy-v", order_index=0,
                    block_type="section_header", text="李晓梅", heading_path="原有路径",
                    sources=[SourceLocation(format="pdf", page=1, section_path="原有路径")]),
              Block(id="legacy-b1", document_id="legacy", parse_version_id="legacy-v", order_index=1,
                    block_type="paragraph", text="认 知 正 文", heading_path="原有路径")]
    before = [block.model_dump() for block in blocks]
    document = NormalizedDocument(document_id="legacy", format="pdf", page_count=1, blocks=blocks)
    chunks = chunk_document(document, ChunkingConfig())
    assert [block.model_dump() for block in document.blocks] == before
    assert all(chunk.heading_path == "原有路径" for chunk in chunks)
    assert any(chunk.text == "认 知 正 文" for chunk in chunks)
    assert RESULT_SCHEMA_VERSION == "docqa-normalized-9"
    assert CHUNKING_ALGO == "structured-heading-table-v6"


@pytest.mark.parametrize("top,sub,task", [
    ("二 、 研 究 方 法", "（ 一 ） 过 程 设 计", "（ 1 ） 观 察 任 务"),
    ("三、研究结果", "( 二 ) 条件核对", "( 2 ) 后续任务"),
])
def test_ocr_spaces_in_numbering_preserve_text_and_recover_hierarchy(top, sub, task):
    document = normalize([heading(top), heading(sub), heading(task), "连续正文内容"])
    expected = " / ".join((top, sub, task))
    assert by_text(document, "连续正文内容").heading_path == expected
    assert by_text(document, task).text == task
    assert by_text(document, sub).heading_path == " / ".join((top, sub))


def test_spaced_reference_boundary_stops_author_names_becoming_headings():
    document = normalize([heading("一 、 结 论"), "结论正文", heading("[ 参 考 文 献 ]"),
                          heading("John Smith"), "期刊信息", heading("Appendix A"), "附录正文"])
    assert by_text(document, "John Smith").block_type == "paragraph"
    assert by_text(document, "期刊信息").heading_path.endswith("[ 参 考 文 献 ]")
    assert "John Smith" not in by_text(document, "期刊信息").heading_path
    assert by_text(document, "附录正文").heading_path.endswith("Appendix A")
