"""从原 PDF 目录和逐页页脚建立地址映射；绝不按固定偏移推算缺页。"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
from pathlib import Path
import re

from app.schemas import PdfPageLabel, QualityWarning

PAGE_LABEL_VERSION = "pdf-page-labels-v1"
_ROMAN = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", re.I)
_LABEL = re.compile(r"(?:[A-Za-z]{1,8}[-.]?)?(?:[1-9][0-9]{0,5})|[ivxlcdmIVXLCDM]{1,15}")


def valid_label(value):
    """只接收短地址；任意 PDF 字符串不能混入引用或模型提示。"""
    return isinstance(value, str) and len(value) <= 32 and bool(_LABEL.fullmatch(value))


def _sequence(label):
    match = re.fullmatch(r"([A-Za-z]{0,8}[-.]?)([1-9][0-9]{0,5})", label)
    if match:
        return "decimal:" + match[1], int(match[2])
    if label and _ROMAN.fullmatch(label):
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        nums = [values[c] for c in label.upper()]
        return "roman:" + ("upper" if label.isupper() else "lower"), sum(-n if i+1 < len(nums) and n < nums[i+1] else n for i, n in enumerate(nums))
    return None


def _catalog_labels(reader, page_count):
    """核验 PDF Number Tree，拒绝第三方库静默回退的默认物理页号。"""
    root = reader.root_object.get("/PageLabels")
    if root is None:
        return {}, "absent"
    entries, visited = [], set()

    def visit(raw, depth=0):
        node = raw.get_object()
        if depth > 20 or id(node) in visited or len(visited) >= 2048 or not isinstance(node, dict):
            raise ValueError("invalid_page_label_tree")
        visited.add(id(node))
        if ("/Nums" in node) == ("/Kids" in node):
            raise ValueError("invalid_page_label_tree")
        if "/Kids" in node:
            kids = node["/Kids"].get_object()
            if not isinstance(kids, list) or not kids:
                raise ValueError("invalid_page_label_tree")
            for child in kids:
                visit(child, depth+1)
            return
        nums = node["/Nums"].get_object()
        if not isinstance(nums, list) or not nums or len(nums) % 2 or len(nums) > 2*page_count:
            raise ValueError("invalid_page_label_tree")
        for index in range(0, len(nums), 2):
            key, value = nums[index].get_object(), nums[index+1].get_object()
            if not isinstance(key, int) or not 0 <= key < page_count or not isinstance(value, dict):
                raise ValueError("invalid_page_label_tree")
            style, start, prefix = value.get("/S"), value.get("/St", 1), value.get("/P", "")
            if style not in {None, "/D", "/r", "/R"} or not isinstance(start, int) or not 1 <= start <= 100000:
                raise ValueError("unsupported_page_label_entry")
            if not isinstance(prefix, str) or len(prefix) > 24 or any(ord(c) < 32 for c in prefix):
                raise ValueError("invalid_page_label_prefix")
            entries.append((key, style, start, prefix))

    def roman(number):
        if not 0 < number < 4000:
            return ""
        result = ""
        for value, token in ((1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),
                             (50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")):
            count, number = divmod(number, value)
            result += token * count
        return result

    try:
        visit(root)
        keys = [entry[0] for entry in entries]
        if not keys or keys[0] != 0 or keys != sorted(set(keys)):
            raise ValueError("invalid_page_label_keys")
        labels = {}
        for n, (first, style, start, prefix) in enumerate(entries):
            end = entries[n+1][0] if n+1 < len(entries) else page_count
            for page in range(first, end):
                number = start + page-first
                suffix = str(number) if style == "/D" else roman(number) if style else ""
                if style == "/r": suffix = suffix.lower()
                if style and not suffix: continue
                label = prefix + suffix
                if valid_label(label): labels[page+1] = label
        return labels, "present" if len(labels) == page_count else "partially_supported"
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return {}, "invalid_or_unsupported"


def _footer_label(block, sizes):
    # 仅接受上游已标为 page_footer、单页、位于底部 12% 的完整地址节点。
    label = block.text.strip()
    match = re.fullmatch(r"(?:[-–—]\s*)?(.+?)(?:\s*[-–—])?", label)
    label = match[1] if match else label
    if not valid_label(label) or not _sequence(label) or len(block.sources) != 1:
        return None
    source = block.sources[0]
    if source.format != "pdf" or not source.node_ref or source.page not in sizes or source.page_end not in {None, source.page}:
        return None
    bbox = source.bbox or {}
    try:
        height = sizes[source.page]
        a, b = float(bbox["t"]), float(bbox["b"])
        if not all(math.isfinite(n) for n in (height, a, b)) or not 0 <= min(a, b) <= max(a, b) <= height:
            return None
        if source.coord_origin == "BOTTOMLEFT":
            bottom = max(a, b) <= height * .12
        elif source.coord_origin == "TOPLEFT":
            bottom = min(a, b) >= height * .88
        else:
            bottom = False
        return (source.page, label, source.node_ref) if bottom else None
    except (TypeError, ValueError, KeyError):
        return None


def apply_pdf_page_labels(document, pdf_path: Path):
    """给本次新解析的来源附加映射；旧版本和原件均不修改。"""
    report = dict(policy=PAGE_LABEL_VERSION, catalog_status="absent", mapped_pages=0,
                  conflict_pages=0, ambiguous_footer_pages=0, status_counts={})
    try:
        from pypdf import PdfReader
        with Path(pdf_path).open("rb") as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted:
                report["catalog_status"] = "unavailable"
                return report
            sizes = {n: float(page.mediabox.height) for n, page in enumerate(reader.pages, 1)}
            catalog, report["catalog_status"] = _catalog_labels(reader, len(sizes))
    except Exception:
        # PDF 不可读时不让可用的 Docling 正文发布失败，也不把异常路径返回给客户。
        report["catalog_status"] = "unavailable"
        return report

    observations = defaultdict(list)
    for block in document.blocks:
        if block.block_type == "page_footer":
            footer = _footer_label(block, sizes)
            if footer:
                observations[footer[0]].append(footer[1:])
    footers = {page: rows[0] for page, rows in observations.items() if len({label for label, _ in rows}) == 1}
    ambiguous = set(observations) - set(footers)
    report["ambiguous_footer_pages"] = len(ambiguous)
    mappings = {}
    for page in sizes:
        catalog_label = catalog.get(page)
        footer = footers.get(page)
        if page in ambiguous:
            continue
        # 无目录时需邻页独立观察到连续标签；不能把孤立年份/金额当页码。
        adjacent = False
        if footer:
            current = _sequence(footer[0])
            for neighbor in (page-1, page+1):
                other = _sequence(footers[neighbor][0]) if neighbor in footers else None
                if current and other and current[0] == other[0] and other[1]-current[1] == neighbor-page:
                    adjacent = True
        if footer and (catalog_label or adjacent):
            status = ("corroborated" if catalog_label == footer[0] else
                      "conflict" if catalog_label else "footer_observed")
            mappings[page] = PdfPageLabel(catalog_label=catalog_label, printed_label=footer[0],
                                         footer_node_ref=footer[1], status=status)
        elif catalog_label:
            mappings[page] = PdfPageLabel(catalog_label=catalog_label, status="catalog_only")
    for block in document.blocks:
        for source in block.sources:
            if source.format == "pdf" and source.page in mappings and source.page_end in {None, source.page}:
                source.pdf_page_label = mappings[source.page].model_copy(deep=True)
    report.update(mapped_pages=len(mappings), conflict_pages=sum(m.status == "conflict" for m in mappings.values()),
                  status_counts=dict(Counter(m.status for m in mappings.values())))
    if report["conflict_pages"] or ambiguous:
        document.warnings.append(QualityWarning(code="pdf_page_label_conflict", severity="warning", scope="document",
            message="部分PDF标签与页脚不一致或页脚有多个候选，这些位置仅使用物理页码；请核对原件"))
    if report["catalog_status"] in {"invalid_or_unsupported", "partially_supported"}:
        document.warnings.append(QualityWarning(code="pdf_page_labels_unavailable", severity="info", scope="document",
            message="部分PDF目录页标签无效或不受支持，未以默认物理页序号充当印刷页码"))
    return report


def requested_page_slots(question, pool):
    """按地址类别召回，不用标签数字匹配正文金额；仅作为候选线索。"""
    from app.rag_numeric_roles import locate
    requests = list(dict.fromkeys(("physical_page" if l.kind == "page" else l.kind, value)
        for l in locate(question) if l.kind in {"page", "physical_page", "printed_page", "pdf_label"}
        for value in l.values))[:16]
    result = []
    for kind, value in requests:
        matched = []
        for i, hit in enumerate(pool):
            for source in hit.get("sources", []):
                if source.get("format") != "pdf":
                    continue
                mapping = source.get("pdf_page_label") or {}
                if kind == "physical_page":
                    page, end = source.get("page"), source.get("page_end") or source.get("page")
                    matches = isinstance(page, int) and isinstance(end, int) and value.isdecimal() and page <= int(value) <= end
                elif mapping.get("status") == "conflict":
                    matches = False
                elif kind == "printed_page":
                    matches = mapping.get("status") in {"footer_observed", "corroborated"} and str(mapping.get("printed_label", "")).casefold() == value
                else:
                    matches = str(mapping.get("catalog_label", "")).casefold() == value
                if matches:
                    matched.append(i)
                    break
        result.append(dict(kind=kind, value=value, indices=matched))
    return result
