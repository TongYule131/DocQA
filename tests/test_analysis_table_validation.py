"""合成的确定性行列反例；这些测试不代替真实模型语义验收。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.analysis_table_validation import validate_table_bindings
from app.analysis_validation import AnalysisOutputError, ValidatedItem


def table_unit(*, ref=1, columns=None, rows=None, header_rows=2):
    columns = columns or [["区域"], ["营业收入", "主营业务"], ["营业收入", "其他业务"],
                          ["成本", "主营业务"], ["净利润"]]
    rows = rows or [["华东", "1234.56", "1100", "700", "98.7"],
                    ["华南", "1100", "900", "600", "88.6"]]
    cells = []
    for col, path in enumerate(columns, 1):
        for row, label in enumerate(path, 1):
            cells.append({"row": row, "col": col, "row_span": 1, "col_span": 1,
                          "text": label, "column_header": True, "row_header": False})
    facts = []
    for row, values in enumerate(rows, header_rows + 1):
        for col, value in enumerate(values, 1):
            cells.append({"row": row, "col": col, "row_span": 1, "col_span": 1,
                          "text": value, "column_header": False, "row_header": False})
            if value:
                facts.append({"row": row, "col": col, "value": value,
                              "column_path": columns[col - 1]})
    structure = {
        "status": "available", "rows": header_rows + len(rows), "cols": len(columns),
        "header_rows": list(range(1, header_rows + 1)), "header_basis": "explicit",
        "cells": cells, "column_paths": [{"col": col, "path": path}
                                           for col, path in enumerate(columns, 1)], "facts": facts,
    }
    return SimpleNamespace(batch_local_id=ref, text="表格原文", kind="table", table_structure=structure)


def data_item(**overrides):
    values = {"item_id": "b1-i1", "kind": "data", "content": "华东主营业务收入1234.56。",
              "name": "主营业务", "scope": "营业收入", "subject": "华东",
              "value_text": "1234.56", "refs": [1]}
    values.update(overrides)
    return ValidatedItem(**values)


def assert_mismatch(item, units):
    with pytest.raises(AnalysisOutputError) as caught:
        validate_table_bindings([item], units)
    assert caught.value.reason == "table_binding_mismatch"
    assert "华东" not in str(caught.value)


def test_multi_level_column_and_subject_are_bound_to_one_cell():
    check = validate_table_bindings([data_item()], [table_unit()])[0]
    assert check == {"item_id": "b1-i1", "status": "verified", "checked_refs": [1],
                     "matched_refs": [1], "verified_fields": ["value_text", "name", "scope", "subject"]}


def test_same_leaf_header_cannot_switch_revenue_to_cost():
    assert_mismatch(data_item(scope="成本"), [table_unit()])


def test_numeric_precision_is_not_normalized():
    assert_mismatch(data_item(value_text="1234.560"), [table_unit()])


def test_number_substring_does_not_match_1100():
    assert_mismatch(data_item(name="其他业务", value_text="110"), [table_unit()])


def test_same_number_elsewhere_cannot_join_subject_and_column_from_different_rows():
    # 1100 在华东的其他业务列、华南的主营业务列；不存在“华东主营业务1100”。
    assert_mismatch(data_item(value_text="1100"), [table_unit()])


def test_column_reordering_uses_paths_instead_of_fixed_column_numbers():
    columns = [["净利润"], ["成本", "主营业务"], ["区域"], ["营业收入", "主营业务"]]
    unit = table_unit(columns=columns, rows=[["98.7", "700", "华东", "1234.56"]])
    assert validate_table_bindings([data_item()], [unit])[0]["status"] == "verified"


def test_adjacent_equal_values_remain_independent_column_facts():
    unit = table_unit(rows=[["华东", "700", "700", "700", "98.7"]])
    items = [data_item(value_text="700"), data_item(item_id="b1-i2", value_text="700", name="其他业务")]
    checks = validate_table_bindings(items, [unit])
    assert [check["status"] for check in checks] == ["verified", "verified"]
    assert len(unit.table_structure["facts"]) == 5


def test_explicit_period_in_other_column_must_be_on_same_row():
    columns = [["区域"], ["年度"], ["营业收入", "主营业务"]]
    unit = table_unit(columns=columns, rows=[["华东", "2024年", "1234.56"],
                                          ["华东", "2023年", "1100"]])
    assert_mismatch(data_item(period="2023年"), [unit])
    assert validate_table_bindings([data_item(period="2024年")], [unit])[0]["status"] == "verified"


def test_body_ref_can_supply_period_and_unit_without_forced_table_row():
    item = data_item(period="2024年", unit="万元", refs=[1, 2])
    text = SimpleNamespace(batch_local_id=2, text="2024年金额单位为万元。", kind="block_type")
    check = validate_table_bindings([item], [table_unit(), text])[0]
    assert check["status"] == "verified"
    assert "period" not in check["verified_fields"]
    assert "unit" not in check["verified_fields"]


def test_arbitrary_scope_is_not_guessed_to_be_a_column_label():
    check = validate_table_bindings([data_item(scope="仅限审计通过项目")], [table_unit()])[0]
    assert check["status"] == "verified"
    assert "scope" not in check["verified_fields"]


def test_explicit_full_path_can_bind_column_without_guessing_words():
    check = validate_table_bindings([data_item(name="营业收入 / 主营业务", scope=None)], [table_unit()])[0]
    assert check["status"] == "verified"


def test_unknown_or_unmarked_header_is_not_inferred():
    unit = table_unit()
    unit.table_structure["header_basis"] = "none"
    check = validate_table_bindings([data_item(scope="成本")], [unit])[0]
    assert check["status"] == "insufficient_metadata"
    assert check["checked_refs"] == []


@pytest.mark.parametrize("structure", [None, {"status": "unavailable"}])
def test_unavailable_or_legacy_table_structure_does_not_reject(structure):
    unit = table_unit()
    unit.table_structure = structure
    assert validate_table_bindings([data_item(scope="成本")], [unit])[0]["status"] == "insufficient_metadata"


def test_missing_formula_cache_stays_empty_without_inventing_zero():
    unit = table_unit(rows=[["华东", "", "", "", ""]])
    item = data_item(value_text="0")
    original = deepcopy(unit.table_structure)
    check = validate_table_bindings([item], [unit])[0]
    assert check["status"] == "insufficient_metadata"
    assert unit.table_structure == original
    assert item.value_text == "0"  # 守卫不修正输出；原文引述校验另行阻止无依据值。


def test_unreferenced_table_cannot_supply_a_valid_binding():
    first = table_unit()
    second = table_unit(ref=2, rows=[["华东", "1234.56", "1100", "1234.56", "98.7"]])
    assert_mismatch(data_item(scope="成本", refs=[1]), [first, second])


def test_other_plain_text_value_cannot_bypass_unique_table_cell():
    text = SimpleNamespace(batch_local_id=2, text="其它项目1234.560万元。", kind="block_type")
    assert_mismatch(data_item(value_text="1234.560", refs=[1, 2]), [table_unit(), text])


def test_multiple_referenced_tables_allow_one_complete_legal_binding():
    first = table_unit()
    second = table_unit(ref=2, rows=[["华东", "1234.56", "1100", "1234.56", "98.7"]])
    check = validate_table_bindings([data_item(scope="成本", refs=[1, 2])], [first, second])[0]
    assert check["status"] == "verified"
    assert check["matched_refs"] == [2]


def test_row_spanning_subject_keeps_explicit_shared_rows():
    columns = [["区域"], ["年度"], ["营业收入", "主营业务"]]
    unit = table_unit(columns=columns, rows=[["华东", "2024年", "1234.56"], ["", "2023年", "1100"]])
    subject_cell = next(cell for cell in unit.table_structure["cells"]
                        if cell["row"] == 3 and cell["col"] == 1)
    subject_cell["row_span"] = 2
    check = validate_table_bindings([data_item(period="2023年", value_text="1100")], [unit])[0]
    assert check["status"] == "verified"


def test_explicit_period_in_column_header_is_bound_to_same_cell():
    columns = [["区域"], ["营业收入", "2024年"], ["营业收入", "2023年"]]
    unit = table_unit(columns=columns, rows=[["华东", "1234.56", "1100"]])
    assert_mismatch(data_item(name="营业收入", scope=None, period="2023年"), [unit])
    check = validate_table_bindings(
        [data_item(name="营业收入", scope=None, period="2024年")], [unit])[0]
    assert check["status"] == "verified"
    assert "period" in check["verified_fields"]


def test_explicit_subject_in_column_header_is_not_guessed_from_first_column():
    columns = [["期间"], ["营业收入", "华东"], ["营业收入", "华南"]]
    unit = table_unit(columns=columns, rows=[["2024年", "1234.56", "1100"]])
    item = data_item(name="营业收入", scope=None, period="2024年", subject="华南")
    assert_mismatch(item, [unit])
    item.subject = "华东"
    assert validate_table_bindings([item], [unit])[0]["status"] == "verified"


def test_dimension_with_explicit_header_and_row_location_can_use_either():
    columns = [["期间"], ["营业收入", "2024年"], ["营业收入", "2023年"]]
    unit = table_unit(columns=columns, rows=[["2023年", "1234.56", "1100"]])
    # 期间也出现在正文第一格，结构没有更详细角色声明时不强制只采用其中一轴。
    item = data_item(name="营业收入", scope=None, period="2023年", subject=None)
    assert validate_table_bindings([item], [unit])[0]["status"] == "verified"


@pytest.mark.parametrize("kind,value", [("conclusion", "1234.56"), ("data", None)])
def test_non_data_or_no_value_is_not_applicable(kind, value):
    check = validate_table_bindings([data_item(kind=kind, value_text=value)], [table_unit()])[0]
    assert check["status"] == "not_applicable"


def test_single_numeric_cell_with_unit_preserves_literal_value():
    unit = table_unit(rows=[["华东", "1234.56万元", "1100", "700", "98.7"]])
    assert validate_table_bindings([data_item(unit="万元")], [unit])[0]["status"] == "verified"


def test_numeric_range_is_not_reduced_to_one_endpoint():
    unit = table_unit(rows=[["华东", "1234.56～2000", "1100", "700", "98.7"]])
    assert_mismatch(data_item(), [unit])
