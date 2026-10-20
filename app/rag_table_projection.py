"""表格行列辅助证据。仅投影已入选原文的坐标，不推断事实或限定适用范围。"""
from __future__ import annotations

import hashlib
import json
import re

from app.chunking import _header_rows, _render_rows, _table_context, _table_rows
from app.schemas import Block

TABLE_PROJECTION_VERSION = "table-coordinate-evidence-v1"
TABLE_PROJECTION_MAX_CHARS = 6000
_TABLE_TYPES = {"table_rows", "document_index", "long_line"}


def table_structure_signature(blocks: list[Block]) -> str:
    # 表格正文未变时，单元格、表头标志、脚注或来源变化也必须使任务快照失效。
    payload = [block.model_dump(mode="json") for block in blocks
               if block.block_type in {"table", "document_index"}]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def table_projection(chunk, block: Block | None) -> dict | None:
    """只接收完整行组且能精确重建的分块；旧文本或残缺结构保留原文回退。"""
    if chunk.chunk_type not in _TABLE_TYPES:
        return None

    def unavailable(reason):
        return {"version": TABLE_PROJECTION_VERSION, "status": "unavailable", "reason": reason}

    if block is None:
        return unavailable("missing_table_block")
    if ((block.document_id, block.parse_version_id, block.id) !=
            (chunk.document_id, chunk.parse_version_id, chunk.block_id)):
        return unavailable("table_scope_mismatch")
    if block.block_type not in {"table", "document_index"} or chunk.chunk_type == "long_line":
        return unavailable("incomplete_table_rows")
    try:
        return _project(chunk, block)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return unavailable("unreliable_table_layout")


def _project(chunk, block):
    table = block.table or {}
    cells = table.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("no cells")
    if any(not isinstance(c, dict) or not isinstance(c.get("text", ""), str) for c in cells):
        raise ValueError("invalid cells")
    grid, _ = _table_rows(block)  # 复用生产分块的坐标、合并格重叠与容量检查。
    header_count = _header_rows(block, grid)
    context = _table_context(block)
    text = chunk.text.replace("\r\n", "\n")
    if header_count == len(grid):
        expected = "\n".join(p for p in [context, _render_rows(grid)] if p).strip()
        visible = list(range(len(grid)))
    else:
        matches = re.findall(r"^行范围：第 (\d+)(?:-(\d+))? 行（表内行号，含表头 (\d+) 行）$", text, re.M)
        if len(matches) != 1:
            raise ValueError("ambiguous row range")
        start, end, count = matches[0]
        first, last = int(start), int(end or start)
        if int(count) != header_count or not header_count < first <= last <= len(grid):
            raise ValueError("invalid range")
        row_range = f"第 {start}{'-' + end if end else ''} 行（表内行号，含表头 {count} 行）"
        expected = "\n".join(p for p in [context, "行范围：" + row_range,
            "表头：" + _render_rows(grid[:header_count]) if header_count else "",
            _render_rows(grid[first - 1:last])] if p).strip()
        visible = list(range(header_count)) + list(range(first - 1, last))
    if text != expected or len(visible) * len(grid[0]) > 400:
        raise ValueError("source text mismatch or capacity")

    declared = {r for c in cells if c.get("column_header") is True
                for r in range(c["row"], c["row"] + c.get("row_span", 1))}
    header_rows = []
    while len(header_rows) in declared and len(header_rows) < len(grid):
        header_rows.append(len(header_rows) + 1)
    paths = []
    for col in range(len(grid[0])):
        path = []
        for c in sorted(cells, key=lambda c: (c["row"], c["col"])):
            if c.get("column_header") is not True or c["row"] + 1 not in header_rows:
                continue
            if c["col"] <= col < c["col"] + c.get("col_span", 1) and c.get("text", "").strip():
                path.append({"row": c["row"] + 1, "col": c["col"] + 1, "text": c["text"].strip()})
        paths.append(path)
    unique_paths = {json.dumps(p, ensure_ascii=False, sort_keys=True) for p in paths}
    header_status = ("unmarked" if not header_rows else
                     "non_discriminating" if len(unique_paths) == 1 else "declared_unverified")
    visible_set = set(visible)
    merges = []
    covered = set()
    for c in cells:
        height, width = c.get("row_span", 1), c.get("col_span", 1)
        if height * width > 1 and visible_set.intersection(range(c["row"], c["row"] + height)):
            merges.append({"row": c["row"] + 1, "col": c["col"] + 1,
                "row_span": height, "col_span": width, "anchor_visible": c["row"] in visible_set})
            covered.update((r, col) for r in range(c["row"], c["row"] + height) if r in visible_set
                           for col in range(c["col"], c["col"] + width) if (r, col) != (c["row"], c["col"]))
    notes = table.get("footnotes") or []
    if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
        raise ValueError("invalid footnotes")

    formulas = []
    origin = re.fullmatch(r"([A-Z]+)([1-9]\d*)(?::[A-Z]+[1-9]\d*)?", str(table.get("cell_range", "")))
    origin_col = 0
    if origin:
        for char in origin[1]:
            origin_col = origin_col * 26 + ord(char) - ord("A") + 1
    for coordinate, info in (table.get("formulas") or {}).items():
        match = re.fullmatch(r"([A-Z]+)([1-9]\d*)", coordinate)
        if not origin or not match:
            raise ValueError("unmapped formula")
        column = 0
        for char in match[1]:
            column = column * 26 + ord(char) - ord("A") + 1
        row, col = int(match[2]) - int(origin[2]), column - origin_col
        if row not in visible_set or not 0 <= col < len(grid[0]):
            continue
        if not isinstance(info, dict) or type(info.get("has_cache")) is not bool:
            raise ValueError("unknown cache state")
        if info["has_cache"] and info.get("cached_value") is None:
            raise ValueError("inconsistent cache state")
        formulas.append({"row": row + 1, "col": col + 1, "cell": coordinate,
            "formula": info.get("formula"), "cache_status": "saved" if info["has_cache"] else "missing",
            "cached_value": info.get("cached_value") if info["has_cache"] else None})
    return {"version": TABLE_PROJECTION_VERSION, "status": "available",
        "basis": "exact_chunk_rows", "total_rows": len(grid), "total_cols": len(grid[0]),
        "rows": [{"row": row + 1, "values": grid[row]} for row in visible],
        "empty_cells": [[r + 1, c + 1] for r in visible for c, value in enumerate(grid[r]) if value == ""],
        "covered_cells": [[r + 1, c + 1] for r, c in sorted(covered)],
        "legend": {"empty": "", "merged_covered": "↳"}, "merges": merges,
        "header_status": header_status, "header_rows": header_rows, "column_paths": paths,
        "footnotes": [{"text": note, "applicability": "unresolved"} for note in notes],
        "formulas": formulas, "condition_scope_verified": False}


def verify_table_snapshot(repository, snapshot: dict) -> list[Block]:
    """新任务的表结构冻结；旧版本检查点仍沿用旧协议。"""
    version = snapshot.get("table_projection_version")
    if version != TABLE_PROJECTION_VERSION:
        raise ValueError("unknown_table_projection_version")
    blocks = repository.blocks(snapshot["parse_version_id"])
    if table_structure_signature(blocks) != snapshot.get("table_structure_signature"):
        raise ValueError("table_structure_changed")
    return blocks


def validate_table_checkpoint(repository, snapshot: dict, selected: list[dict], citations) -> None:
    """恢复发布重新绑定原始表格及正文；不信任可变检查点中的派生结构。"""
    from app.vector_index import evidence_signature
    blocks = {b.id: b for b in verify_table_snapshot(repository, snapshot)}
    chunks = repository.chunks(snapshot["document_id"], snapshot["parse_version_id"])
    if evidence_signature(chunks) != snapshot.get("evidence_signature"):
        raise ValueError("table_evidence_source_changed")
    by_id = {chunk.id: chunk for chunk in chunks}
    refs = {}
    for item in selected:
        chunk = by_id.get(item.get("chunk_id"))
        ref = item.get("reference_id")
        if type(ref) is not int or ref < 1 or ref in refs or chunk is None:
            raise ValueError("table_checkpoint_source_mismatch")
        if (item.get("text"), item.get("sources"), item.get("document_id"), item.get("parse_version_id"), item.get("page")) != (
                chunk.text.replace("\r\n", "\n"), [s.model_dump() for s in chunk.sources],
                chunk.document_id, chunk.parse_version_id, chunk.page):
            raise ValueError("table_checkpoint_source_mismatch")
        projection = table_projection(chunk, blocks.get(chunk.block_id))
        included = item.get("table_projection_included")
        if type(included) is not bool or projection != item.get("table_projection"):
            raise ValueError("table_checkpoint_projection_mismatch")
        if snapshot.get("qualifier_cue_version"):
            from app.rag_qualifier_cues import QUALIFIER_CUE_VERSION, qualifier_cues
            expected_cues = qualifier_cues(chunk.text)
            if (snapshot["qualifier_cue_version"] != QUALIFIER_CUE_VERSION
                    or item.get("qualifier_cues") != expected_cues
                    or type(item.get("qualifier_cues_included")) is not bool
                    or (item["qualifier_cues_included"] and not expected_cues)):
                raise ValueError("qualifier_checkpoint_mismatch")
        if included and (not projection or projection["status"] != "available"
                         or len(json.dumps(projection, ensure_ascii=False, separators=(",", ":"))) > TABLE_PROJECTION_MAX_CHARS):
            raise ValueError("table_checkpoint_projection_unavailable")
        refs[ref] = projection if included else None
    if any(c.table_structure != refs.get(c.reference_id) for c in citations):
        raise ValueError("table_result_projection_mismatch")
