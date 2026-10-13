"""保守核对提取项与显式表格行列的绑定，不推断表头、单位或缺失值。

本模块只使用解析器保存、输入规划确认可用的结构。它能拒绝“数值和主体、
表头各自存在，但不属于同一格”的明确矛盾，不能证明全部业务事实或选取完整。
字段不足时保留原输出并返回 insufficient_metadata；不生成或修正模型事实。
"""
from __future__ import annotations

import re
from typing import Any


_NUMBER = re.compile(r"[-+−]?\d+(?:,\d{3})*(?:\.\d+)?(?:[%％])?")
_NUMBER_IN_CELL = re.compile(
    r"(?<![\d.,])[-+−]?\d+(?:,\d{3})*(?:\.\d+)?(?:[%％])?(?![\d.,])"
)
_PATH_SEPARATOR = re.compile(r"\s*(?:>|＞|→|/|／|\||｜|:|：)\s*")


def _literal(value: Any) -> str:
    """与既有事实字段相同，只允许空白差异，不转换数字、单位或措辞。"""
    return "".join(value.split()) if isinstance(value, str) else ""


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _value_matches(claim: str, cell: str, unit: str | None) -> bool:
    expected, actual = _literal(claim), _literal(cell)
    if expected == actual:
        return bool(expected)
    if not _NUMBER.fullmatch(expected):
        return False
    # 允许单个数字带文字单位，但不能从范围、日期或多个数值中抽一个数来匹配。
    # 比较原数字文本，10 不等于 110、10.0 或 10.00，也不把缺缓存当作 0。
    tokens = _NUMBER_IN_CELL.findall(actual)
    if len(tokens) != 1:
        return False
    token = tokens[0]
    if token == expected:
        return True
    return _literal(unit) in {"%", "％"} and token == expected + _literal(unit)


def _structure(unit: Any) -> dict[str, Any] | None:
    structure = getattr(unit, "table_structure", None)
    if not isinstance(structure, dict) or structure.get("status") != "available":
        return None
    if not _positive_int(structure.get("rows")) or not _positive_int(structure.get("cols")):
        return None
    if structure.get("header_basis") not in {"explicit", "explicit_and_grouped_subheaders"}:
        return None
    if not structure.get("header_rows") or not isinstance(structure.get("cells"), list):
        return None
    if not isinstance(structure.get("facts"), list) or not isinstance(structure.get("column_paths"), list):
        return None
    return structure


def _paths(structure: dict[str, Any]) -> dict[int, tuple[str, ...]]:
    paths: dict[int, tuple[str, ...]] = {}
    for entry in structure["column_paths"]:
        if not isinstance(entry, dict) or not _positive_int(entry.get("col")):
            continue
        path = entry.get("path")
        if not isinstance(path, list) or not path or not all(_literal(label) for label in path):
            continue
        paths[entry["col"]] = tuple(_literal(label) for label in path)
    return paths


def _column_constraints(item: Any, paths: dict[int, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    known_labels = {label for path in paths.values() for label in path}
    constraints: dict[str, tuple[str, ...]] = {}
    for field in ("name", "scope"):
        text = getattr(item, field, None)
        compact = _literal(text)
        if compact in known_labels:
            constraints[field] = (compact,)
            continue
        # 只认明确的表头路径写法。不把“仅限主营业务”等任意口径文字猜成表头。
        parts = tuple(_literal(part) for part in _PATH_SEPARATOR.split(text or ""))
        if len(parts) > 1 and parts in paths.values():
            constraints[field] = parts
    return constraints


def _path_contains(path: tuple[str, ...], labels: tuple[str, ...]) -> bool:
    return any(path[start:start + len(labels)] == labels
               for start in range(len(path) - len(labels) + 1))


def _row_constraints(item: Any, structure: dict[str, Any]) -> dict[str, set[int]]:
    headers = set(structure["header_rows"])
    constraints: dict[str, set[int]] = {}
    for field in ("subject", "period"):
        claim = _literal(getattr(item, field, None))
        if not claim:
            continue
        matching_rows: set[int] = set()
        for cell in structure["cells"]:
            if not isinstance(cell, dict) or cell.get("column_header"):
                continue
            row, span = cell.get("row"), cell.get("row_span", 1)
            if not _positive_int(row) or not _positive_int(span) or row in headers:
                continue
            if _literal(cell.get("text")) == claim:
                matching_rows.update(candidate for candidate in range(row, row + span)
                                     if candidate <= structure["rows"] and candidate not in headers)
        # 时间或主体可以来自同条引用的正文。表内找不到时不臆造对应行。
        if matching_rows:
            constraints[field] = matching_rows
    return constraints


def _header_dimensions(item: Any, paths: dict[int, tuple[str, ...]]) -> dict[str, set[int]]:
    """主体、期间也可以明确横排；只按原文表头字面定位，不推断标题的角色。"""
    dimensions: dict[str, set[int]] = {}
    for field in ("subject", "period"):
        claim = _literal(getattr(item, field, None))
        if not claim:
            continue
        columns = {col for col, path in paths.items() if claim in path}
        if columns:
            dimensions[field] = columns
    return dimensions


def _facts(structure: dict[str, Any], paths: dict[int, tuple[str, ...]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    headers = set(structure["header_rows"])
    for fact in structure["facts"]:
        if not isinstance(fact, dict):
            continue
        row, col = fact.get("row"), fact.get("col")
        if (not _positive_int(row) or not _positive_int(col) or row in headers
                or row > structure["rows"] or col > structure["cols"] or col not in paths):
            continue
        path = fact.get("column_path")
        if not isinstance(path, list) or tuple(_literal(label) for label in path) != paths[col]:
            continue
        if _literal(fact.get("value")):
            facts.append(fact)
    return facts


def _check_table(item: Any, structure: dict[str, Any]) -> tuple[str, list[str]]:
    """返回 support / contradiction / insufficient 和明确核对的字段名。"""
    paths = _paths(structure)
    columns = _column_constraints(item, paths)
    rows = _row_constraints(item, structure)
    header_dimensions = _header_dimensions(item, paths)
    dimensions = list(dict.fromkeys([*rows, *header_dimensions]))
    facts = _facts(structure, paths)
    if not columns or not facts:
        return "insufficient", []

    def matches_context(fact: dict[str, Any]) -> bool:
        return (all(_path_contains(paths[fact["col"]], labels) for labels in columns.values())
                and all(fact["row"] in rows.get(field, set())
                        or fact["col"] in header_dimensions.get(field, set())
                        for field in dimensions))

    matching_values = [fact for fact in facts
                       if _value_matches(item.value_text, fact["value"], getattr(item, "unit", None))]
    matching_context = [fact for fact in facts if matches_context(fact)]
    if any(matches_context(fact) for fact in matching_values):
        return "support", ["value_text", *columns, *dimensions]
    if matching_values:
        # 数值在已引用的表内，但明确表头/同行字面条件无法同时成立。
        return "contradiction", []
    # 主体和指标已唯一定位一个有值的单元格时，不能借正文其它 ref 的数值绕过它。
    # 不用单独表头或单独行标签推断“不存在”：正文可能提供时间或另一口径。
    if "subject" in dimensions and len(matching_context) == 1:
        return "contradiction", []
    return "insufficient", []


def validate_table_bindings(items: list[Any], units: list[Any]) -> list[dict[str, Any]]:
    """核对实际 refs 中可用表格，返回有限的绑定证据状态，不改写 items。

    每项结果字段为 item_id、status、checked_refs、matched_refs、verified_fields。
    verified 只表示列和能识别的同行条件存在同格支撑，不代表全部字段或完整性通过。
    多个表可以分别提供依据；至少一个表有合法绑定时，不因另一个表的局部信息拒绝。
    """
    units_by_ref = {unit.batch_local_id: unit for unit in units}
    results: list[dict[str, Any]] = []
    for item in items:
        result: dict[str, Any] = {
            "item_id": item.item_id, "status": "not_applicable", "checked_refs": [],
            "matched_refs": [], "verified_fields": [],
        }
        results.append(result)
        if item.kind != "data" or not _literal(getattr(item, "value_text", None)):
            continue
        referenced_units = [units_by_ref[ref] for ref in item.refs if ref in units_by_ref]
        if not any(getattr(unit, "kind", None) == "table"
                   or getattr(unit, "table_structure", None) is not None for unit in referenced_units):
            continue
        result["status"] = "insufficient_metadata"
        contradictory = False
        for unit in referenced_units:
            structure = _structure(unit)
            if structure is None:
                continue
            result["checked_refs"].append(unit.batch_local_id)
            status, fields = _check_table(item, structure)
            if status == "support":
                result["matched_refs"].append(unit.batch_local_id)
                if len(fields) > len(result["verified_fields"]):
                    result["verified_fields"] = fields
            elif status == "contradiction":
                contradictory = True
        if result["matched_refs"]:
            result["status"] = "verified"
        elif contradictory:
            # 延迟导入避免 analysis_validation 调用本函数时的循环依赖。
            from app.analysis_validation import AnalysisOutputError
            raise AnalysisOutputError(
                "table_binding_mismatch", "数据项的数值与明确表头或同行主体、时间不一致")
    return results
