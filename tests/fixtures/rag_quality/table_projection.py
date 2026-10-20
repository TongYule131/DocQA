"""独立冻结的合成表格场景；期望只标结构，不猜期间或脚注适用关系。"""
from app.schemas import Block


def cell(row, col, text, **extra):
    return dict(row=row, col=col, text=text, **extra)


def cases():
    definitions = [
        ("layered", 4, 3, [cell(0, 0, "指标", row_span=2, column_header=True),
            cell(0, 1, "金额（万元）", col_span=2, column_header=True),
            cell(1, 1, "2024", column_header=True), cell(1, 2, "2025", column_header=True),
            cell(2, 0, "收入"), cell(2, 1, "9"), cell(2, 2, "9"),
            cell(3, 0, "退款"), cell(3, 1, ""), cell(3, 2, "0")],
         {"header_status": "declared_unverified", "header_rows": [1, 2], "footnotes": ["*部分项目未经审计"]}),
        ("banner", 3, 3, [cell(0, 0, "经营情况", col_span=3, column_header=True),
            cell(1, 0, "指标"), cell(1, 1, "2024万元"), cell(1, 2, "2025万元"),
            cell(2, 0, "收入"), cell(2, 1, "9"), cell(2, 2, "9")],
         {"header_status": "non_discriminating", "header_rows": [1]}),
        ("unmarked", 2, 2, [cell(0, 0, "指标"), cell(0, 1, "本年"),
            cell(1, 0, "补助"), cell(1, 1, "")],
         {"header_status": "unmarked", "header_rows": []}),
        ("formulas", 3, 3, [cell(0, 0, "项目", column_header=True),
            cell(0, 1, "缺缓存", column_header=True), cell(0, 2, "缓存零", column_header=True),
            cell(1, 0, "差额"), cell(1, 1, ""), cell(1, 2, "0"),
            cell(2, 0, "实际零"), cell(2, 1, "0"), cell(2, 2, "")],
         {"header_status": "declared_unverified", "header_rows": [1], "formulas": {
            "B2": {"formula": "=1-1", "has_cache": False, "cached_value": None},
            "C2": {"formula": "=1-1", "has_cache": True, "cached_value": 0}}}),
        ("row_groups", 9, 2, [cell(0, 0, "指标", column_header=True), cell(0, 1, "金额", column_header=True)]
            + [cell(r, c, f"行{r}" if c == 0 else str(r)) for r in range(1, 9) for c in range(2)],
         {"header_status": "declared_unverified", "header_rows": [1]}),
        ("cross_group_merge", 5, 2, [cell(0, 0, "主体", column_header=True),
            cell(0, 1, "金额", column_header=True), cell(1, 0, "甲组", row_span=4)]
            + [cell(r, 1, str(r)) for r in range(1, 5)],
         {"header_status": "declared_unverified", "header_rows": [1]}),
        ("header_only", 1, 2, [cell(0, 0, "项目", column_header=True), cell(0, 1, "单位", column_header=True)],
         {"header_status": "declared_unverified", "header_rows": [1]}),
    ]
    result = []
    for name, rows, cols, cells, expected in definitions:
        table = {"num_rows": rows, "num_cols": cols, "cells": cells, "cell_range": f"A1:C{rows}"}
        table.update({key: expected[key] for key in ("footnotes", "formulas") if key in expected})
        block = Block(id=f"table-{name}", document_id="synthetic-tables", parse_version_id="tables-v1",
            order_index=0, block_type="table", text="合成表格", sheet_name="经营", table=table)
        result.append({"id": name, "block": block, "expected": expected})
    return result
