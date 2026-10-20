"""合成表格验证：列绑定、合并关系及异常结构不依赖真实文档或在线 API。"""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.analysis_prompts import build_batch_user_prompt
from app.analysis_sources import (
    INPUT_PROTOCOL_VERSION, batch_message_chars, build_units, plan_batches,
    plan_fingerprint, render_table_text,
)
from app.config import Settings
from app.schemas import Block, SourceLocation


def block(cells=None, **table):
    value = dict(table)
    if cells is not None:
        value["cells"] = cells
    return Block(id="synthetic-table", document_id="synthetic-doc",
                 parse_version_id="synthetic-version", order_index=1,
                 block_type="table", text="无结构回退文本\n第二行",
                 table=value, sources=[SourceLocation(format="xlsx", sheet_name="合成工作表")])


def cell(row, col, text, **options):
    return {"row": row, "col": col, "text": text, **options}


def layered_cells():
    # 真实解析器可能只给最上层标记 column_header，第二层必须保守识别。
    return [
        cell(0, 0, "区域", column_header=True),
        cell(0, 1, "收入", column_header=True, col_span=2),
        cell(0, 3, "成本", column_header=True, col_span=2),
        cell(0, 5, "利润", column_header=True),
        cell(1, 0, ""), cell(1, 1, "主要业务"), cell(1, 2, "附属业务"),
        cell(1, 3, "主要业务"), cell(1, 4, "附属业务"), cell(1, 5, ""),
        cell(2, 0, "甲区"), cell(2, 1, "21"), cell(2, 2, "19"),
        cell(2, 3, "9"), cell(2, 4, "9"), cell(2, 5, "2"),
    ]


def structure_for(table_block):
    units, _ = build_units([table_block])
    assert len(units) == 1
    return units[0], units[0].table_structure


def test_merged_headers_preserve_slots_hierarchy_and_repeated_values():
    source = block(layered_cells(), num_rows=3, num_cols=6, sheet_name="分区经营")
    original = deepcopy(source.model_dump())
    unit, structure = structure_for(source)
    assert "区域 | 收入 | ↳ | 成本 | ↳ | 利润" in unit.text
    assert "甲区 | 21 | 19 | 9 | 9 | 2" in unit.text
    assert "工作表：分区经营" in unit.text
    assert structure["header_rows"] == [1, 2]
    assert structure["header_basis"] == "explicit_and_grouped_subheaders"
    paths = {entry["col"]: entry["path"] for entry in structure["column_paths"]}
    assert paths[2] == ["收入", "主要业务"]
    assert paths[3] == ["收入", "附属业务"]
    assert paths[5] == ["成本", "附属业务"]
    assert {fact["col"] for fact in structure["facts"] if fact["value"] == "9"} == {4, 5}
    assert source.model_dump() == original


def test_row_spans_holes_and_empty_values_have_distinct_rendering():
    source = block([
        cell(0, 0, "名称", column_header=True, row_span=2),
        cell(0, 1, "数额", column_header=True), cell(0, 2, "附注", column_header=True),
        cell(1, 1, "21"), cell(1, 2, ""), cell(2, 2, "尾项"),
    ], num_rows=4, num_cols=3)
    unit, structure = structure_for(source)
    assert "名称 | 数额 | 附注\n↳ | 21 | （空）\n（空） | （空） | 尾项\n（空） | （空） | （空）" in unit.text
    assert structure["cells"][0]["row_span"] == 2


def test_only_explicit_header_rows_are_trusted_without_grouped_subheader_geometry():
    unit, structure = structure_for(block([
        cell(0, 0, "区域", column_header=True),
        cell(0, 1, "收入", column_header=True, col_span=2),
        cell(1, 0, "甲区"), cell(1, 1, "主要业务"), cell(1, 2, "附属业务"),
    ]))
    assert structure["header_rows"] == [1]
    assert structure["header_basis"] == "explicit"
    assert any(fact["row"] == 2 for fact in structure["facts"])


@pytest.mark.parametrize("last_values", [("21", "19"), ("主要业务", ""), ("同一业务", "同一业务")])
def test_numeric_incomplete_or_duplicate_children_do_not_imply_extra_header(last_values):
    _, structure = structure_for(block([
        cell(0, 0, "区域", column_header=True),
        cell(0, 1, "收入", column_header=True, col_span=2),
        cell(1, 0, ""), cell(1, 1, last_values[0]), cell(1, 2, last_values[1]),
    ]))
    assert structure["header_rows"] == [1]


def test_explicit_equal_words_on_different_header_levels_are_not_deduplicated():
    _, structure = structure_for(block([
        cell(0, 0, "总额", column_header=True, col_span=2),
        cell(1, 0, "总额", column_header=True), cell(1, 1, "其中", column_header=True),
        cell(2, 0, "21"), cell(2, 1, "19"),
    ]))
    assert structure["column_paths"][0]["path"] == ["总额", "总额"]


def test_three_level_explicit_and_geometric_header_paths_preserve_all_levels():
    _, structure = structure_for(block([
        cell(0, 0, "分类", column_header=True),
        cell(0, 1, "综合", column_header=True, col_span=4),
        cell(1, 1, "收入", col_span=2), cell(1, 3, "成本", col_span=2),
        cell(2, 1, "主要"), cell(2, 2, "附属"),
        cell(2, 3, "主要"), cell(2, 4, "附属"),
        cell(3, 0, "甲区"), cell(3, 1, "21"), cell(3, 2, "19"),
        cell(3, 3, "9"), cell(3, 4, "8"),
    ]))
    assert structure["header_rows"] == [1, 2, 3]
    assert structure["column_paths"][4]["path"] == ["综合", "成本", "附属"]
    assert next(fact for fact in structure["facts"] if fact["value"] == "8")["row"] == 4


def test_row_header_marker_prevents_geometric_subheader_inference():
    _, structure = structure_for(block([
        cell(0, 0, "类别", column_header=True, col_span=2),
        cell(1, 0, "甲类", row_header=True), cell(1, 1, "乙类"),
    ]))
    assert structure["header_rows"] == [1]


def test_unknown_header_metadata_does_not_make_first_row_a_header():
    _, structure = structure_for(block([cell(0, 0, "甲区"), cell(0, 1, "21")]))
    assert structure["status"] == "available"
    assert structure["header_rows"] == []
    assert structure["column_paths"] == []
    assert structure["facts"] == []


def test_column_permutation_moves_the_complete_path_with_the_value():
    _, original = structure_for(block(layered_cells()))
    swapped = []
    # 把收入、成本两个跨列组互换，主体列/利润列保持原位。
    for value in layered_cells():
        copied = dict(value)
        copied["col"] = {1: 3, 2: 4, 3: 1, 4: 2}.get(value["col"], value["col"])
        swapped.append(copied)
    _, permuted = structure_for(block(swapped))
    for structure, expected_col in ((original, 3), (permuted, 5)):
        fact = next(value for value in structure["facts"] if value["value"] == "19")
        assert fact["col"] == expected_col
        assert fact["column_path"] == ["收入", "附属业务"]


@pytest.mark.parametrize("cells,table,reason", [
    ([cell(-1, 0, "证据")], {}, "invalid_cell_coordinates"),
    ([cell(True, 0, "证据")], {}, "invalid_cell_coordinates"),
    ([cell(0, None, "证据")], {}, "invalid_cell_coordinates"),
    ([cell(0, 0, "证据", col_span=0)], {}, "invalid_cell_coordinates"),
    ([cell(0, 0, "证据", row_span=True)], {}, "invalid_cell_coordinates"),
    ([cell(0, 0, "证据", col_span=10**9)], {}, "table_grid_too_large"),
    ([cell(10**9, 0, "证据")], {}, "table_grid_too_large"),
    ([cell(0, 0, "证据")], {"num_rows": 10**9}, "table_grid_too_large"),
    ([cell(0, 0, "证据")], {"num_cols": False}, "invalid_table_dimensions"),
    ([cell(1, 0, "证据")], {"num_rows": 1}, "cell_outside_table_dimensions"),
    ([cell(0, 0, "证据", col_span=2), cell(0, 1, "另一证据")], {}, "overlapping_cells"),
])
def test_bad_table_structures_degrade_without_covering_or_inventing_cells(cells, table, reason):
    unit, structure = structure_for(block(cells, **table))
    assert structure["status"] == "unavailable"
    assert structure["reason"] == reason
    assert structure["facts"] == []
    for value in cells:
        assert value["text"] in unit.text


def test_no_cells_falls_back_to_original_text_without_fake_structure():
    source = block(sheet_name="元数据工作表")
    unit, structure = structure_for(source)
    assert structure is None
    assert "无结构回退文本\n第二行" in unit.text
    assert "table_structure" not in unit.payload()


@pytest.mark.parametrize("cells", [{"unexpected": "形状异常"}, ["形状异常"]])
def test_non_list_or_non_mapping_cells_do_not_crash_projection(cells):
    unit, structure = structure_for(block(cells))
    assert structure["status"] == "unavailable"
    assert "无结构回退文本\n第二行" in unit.text


def test_compact_payload_keeps_relative_structure_and_counts_it_in_budget():
    unit, structure = structure_for(block(layered_cells(), num_rows=3, num_cols=6))
    payload = unit.payload()
    sent = payload["table_structure"]
    assert sent["merged_cells"][0]["col"] == 2
    assert "cells" not in sent and "facts" not in sent
    assert "synthetic-table" not in json.dumps(payload, ensure_ascii=False)
    actual = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    batches, unplannable = plan_batches([unit], batch_max_chars=actual + 2)
    assert batches == [[unit]] and not unplannable
    batches, unplannable = plan_batches([unit], batch_max_chars=actual + 1)
    assert not batches and len(unplannable) == 1
    user_prompt = build_batch_user_prompt(kind="extraction", document_name="合成",
                                         version_id="synthetic-version",
                                         units=[{**payload, "ref": 1}], batch_index=1, batch_total=1)
    assert batch_message_chars(kind="extraction", document_name="合成",
                               version_id="synthetic-version", units=[unit],
                               system_prompt_chars=73) == len(user_prompt) + 73


def test_plan_fingerprint_changes_when_structure_metadata_changes():
    unit, _ = structure_for(block(layered_cells()))
    options = dict(kind="extraction", version_id="synthetic-version", units=[unit],
                   batches=[[unit]], settings=Settings(), prompt_version="synthetic")
    before = plan_fingerprint(**options)
    unit.table_structure["column_paths"][1]["path"] = ["不同层级"]
    assert plan_fingerprint(**options) != before
    assert INPUT_PROTOCOL_VERSION == "docqa-analysis-input-v3"
