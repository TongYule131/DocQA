"""Input-only OCR region discovery and vector-grid candidates; no gold or DB access."""
from __future__ import annotations

import copy
import re


def normalized(text):
    return "".join(text.split())


def intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def native_reliable(native, minimum=40):
    text = normalized(native["text"])
    bad = sum(ch == "\ufffd" or (ord(ch) < 32) for ch in text)
    return len(text) >= minimum and bad == 0 and bool(native["lines"])


def tile_boxes(rows=2, cols=2, overlap=.15):
    if rows < 1 or cols < 1 or not 0 <= overlap < .5:
        raise ValueError("Invalid tile geometry")
    return [[max(0, c / cols - overlap / cols), max(0, r / rows - overlap / rows),
             min(1, (c + 1) / cols + overlap / cols), min(1, (r + 1) / rows + overlap / rows)]
            for r in range(rows) for c in range(cols)]


def discover_uncovered(baseline_lines, candidates, minimum_confidence=.92, maximum=40):
    """Keep only independently detected novel boxes; conflicting text stays a review record."""
    accepted, records = [], []
    for line in sorted(candidates, key=lambda row: -row["confidence"]):
        record = copy.deepcopy(line)
        if not normalized(line["text"]) or area(line["bbox"]) <= 0:
            reason = "empty"
        elif line.get("touches_tile_edge"):
            reason = "tile_edge"
        elif line["confidence"] < minimum_confidence:
            reason = "low_confidence"
        else:
            # Native glyph boxes can be much thinner than OCR line boxes. A one-way
            # candidate-area ratio misses existing text fully contained in the crop.
            # Bidirectional containment deliberately abstains on partially known lines.
            overlaps = [other for other in baseline_lines + accepted
                        if intersection(line["bbox"], other["bbox"]) / max(area(line["bbox"]), 1e-12) >= .35
                        or intersection(line["bbox"], other["bbox"]) / max(area(other["bbox"]), 1e-12) >= .65]
            if overlaps:
                reason = "already_covered" if any(normalized(other["text"]) == normalized(line["text"]) for other in overlaps) else "overlap_disagreement"
            elif len(accepted) >= maximum:
                reason = "page_cap"
            else:
                reason = "new_region_candidate"
        record.update(reason=reason, accepted=reason == "new_region_candidate", review_required=reason in {"new_region_candidate", "overlap_disagreement"})
        if record['accepted']:
            accepted.append(copy.deepcopy(record))
        records.append(record)
    return accepted, records


def infer_header_end(cells, rows):
    """Stop header at first account-code/total row, otherwise abstain."""
    for r in range(min(rows, 8)):
        starts = [c for c in cells if c["r0"] == r and c["c0"] == 0]
        if len(starts) != 1:
            continue
        value = normalized(starts[0]["text"])
        if re.fullmatch(r"\d{3,}", value) or any(word in value for word in ["合计", "总计"]):
            return r if r > 0 else None
    return None


def vector_table_candidate(table):
    """Require complete nonoverlapping vector topology and explicit header boundary."""
    rows, cols = table["rows"], table["cols"]
    if not 3 <= rows <= 500 or not 2 <= cols <= 100:
        return None, "unsupported_shape"
    occupied = set()
    for cell in table["cells"]:
        if not 0 <= cell["r0"] < cell["r1"] <= rows or not 0 <= cell["c0"] < cell["c1"] <= cols:
            return None, "invalid_span"
        for r in range(cell["r0"], cell["r1"]):
            for c in range(cell["c0"], cell["c1"]):
                if (r, c) in occupied:
                    return None, "overlapping_cells"
                occupied.add((r, c))
    if len(occupied) != rows * cols:
        return None, "incomplete_grid"
    header_end = infer_header_end(table["cells"], rows)
    if header_end is None:
        return None, "ambiguous_header"
    result = copy.deepcopy(table)
    result["header_end"] = header_end
    for cell in result["cells"]:
        cell["column_header"] = cell["r0"] < header_end
        cell["kind"] = "cell"
        cell["table_id"] = table["id"]
    return result, "vector_grid_candidate"


def apply_vector_candidates(prediction, native):
    result = copy.deepcopy(prediction)
    result["vector_records"] = []
    if not native_reliable(native):
        return result
    used = set()
    for table in native["tables"]:
        candidate, reason = vector_table_candidate(table)
        record = {"id": table["id"], "reason": reason, "accepted": False}
        if candidate is not None:
            matches = []
            for i, old in enumerate(result["tables"]):
                both = intersection(table["bbox"], old["bbox"])
                overlap = both / max(area(table["bbox"]) + area(old["bbox"]) - both, 1e-12)
                if overlap >= .6 and i not in used:
                    matches.append(i)
            if len(matches) == 1:
                index = matches[0]
                old = result["tables"][index]
                # 表格候选改变内容/网格，不改变文档树中的来源身份和位置。
                candidate["candidate_id"] = candidate["id"]
                candidate["id"] = old["id"]
                candidate["order"] = old["order"]
                candidate["replaces"] = old["id"]
                for ci, cell in enumerate(candidate["cells"]):
                    cell["id"] = f'{old["id"]}/cell-{ci}'
                    cell["table_id"] = old["id"]
                    cell["order"] = old["order"]
                result["tables"][index] = candidate
                units, inserted = [], False
                for unit in result["units"]:
                    if unit["kind"] == "table" and unit["id"] == old["id"]:
                        units.append({**unit, "bbox": candidate["bbox"], "order": old["order"]})
                        units.extend(copy.deepcopy(candidate["cells"]))
                        inserted = True
                    elif unit.get("table_id") != old["id"]:
                        units.append(unit)
                if not inserted:
                    units.extend([{"id": old["id"], "kind": "table", "bbox": candidate["bbox"],
                                   "order": old["order"], "text": ""}, *copy.deepcopy(candidate["cells"])])
                result["units"] = units
                used.add(index)
                result["review_required"] = True
                record.update(accepted=True, replaces=old["id"], rows=candidate["rows"], cols=candidate["cols"], header_end=candidate["header_end"])
            else:
                record["reason"] = "missing_or_ambiguous_table_match"
        result["vector_records"].append(record)
    return result
