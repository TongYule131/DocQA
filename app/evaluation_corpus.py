"""提升工程的独立、零供应商调用评估底座。

只校验合成资料及已保存的人工复核记录，不加载 Settings，不解析正式资料，
不将源稿标注、模拟响应或词面匹配说成真实模型语义通过。
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any
from xml.etree import ElementTree as ET
import zipfile

SCHEMA_VERSION = "docqa-improvement-corpus-v1"
CATEGORIES = ("two_column_paper", "long_report", "policy_clauses", "complex_docx", "complex_xlsx", "scan_mixed_pdf")
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "improvement"
DEFAULT_CORPUS = Path(__file__).resolve().parents[1] / "data" / "improvement-20261018" / "corpus"
W = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
S = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class CorpusError(ValueError):
    """清单、文件、来源或复核记录不满足冻结工程协议。"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contained_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise CorpusError("清单路径越过资料根目录")
    return path


def load_manifest(root: Path = FIXTURE_ROOT) -> dict[str, Any]:
    manifest = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("provider_calls") != 0:
        raise CorpusError("不支持的清单版本或非零供应商调用声明")
    documents = manifest.get("documents", [])
    if len(documents) != 18:
        raise CorpusError("必须有18份资料")
    identifiers: set[str] = set()
    scenarios: set[str] = set()
    families: dict[str, str] = {}
    templates: dict[str, str] = {}
    for doc in documents:
        doc_id = doc["id"]
        if doc_id in identifiers:
            raise CorpusError("重复文档编号")
        identifiers.add(doc_id)
        if doc["split"] not in {"dev", "holdout"} or doc["category"] not in CATEGORIES:
            raise CorpusError("未知分组或文档类别")
        for mapping, key in ((families, "source_family"), (templates, "template_family")):
            family = doc[key]
            if family in mapping and mapping[family] != doc["split"]:
                raise CorpusError("同源或模板跨开发与盲测分组")
            mapping[family] = doc["split"]
        source = doc["provenance"]
        if source.get("kind") != "synthetic" or source.get("license") != "CC0-1.0" or not source.get("source") or not source.get("warning"):
            raise CorpusError("合成来源与许可信息不完整")
        if not re.fullmatch(r"[0-9a-f]{64}", doc["sha256"]):
            raise CorpusError("非法文件哈希")
        contained_path(FIXTURE_ROOT, doc["asset"])
        facts = {f["id"]: f for f in doc["facts"]}
        if len(facts) != len(doc["facts"]) or len(facts) < 9:
            raise CorpusError("重复事实或事实标注不足")
        for fact in facts.values():
            if not fact.get("quote") or not fact.get("locator"):
                raise CorpusError("原文或定位为空")
        if Counter(s["task"] for s in doc["scenarios"]) != {"qa": 6, "summary": 1, "extraction": 1}:
            raise CorpusError("每文档需要6问答、1摘要、1提取")
        for scenario in doc["scenarios"]:
            if scenario["id"] in scenarios:
                raise CorpusError("重复场景编号")
            scenarios.add(scenario["id"])
            required = scenario["required_fact_ids"]
            if not required or len(set(required)) != len(required) or not set(required) <= facts.keys():
                raise CorpusError("场景引用未知或重复的必须事实")
            if not scenario.get("request") or not scenario.get("forbidden_facts"):
                raise CorpusError("问题或禁止推断为空")
            if not scenario.get("evidence_groups") or any(not group or not set(group) <= facts.keys() for group in scenario["evidence_groups"]):
                raise CorpusError("证据组为空或来源不属于本文件")
    for category in CATEGORIES:
        if Counter(d["split"] for d in documents if d["category"] == category) != {"dev": 2, "holdout": 1}:
            raise CorpusError("每类必须2份开发与1份盲测")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _cell_value(archive: zipfile.ZipFile, sheet: str, cell: str) -> str:
    """按实际工作表名解析OOXML，只读来源；不执行公式、不补缓存。"""
    book = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    links = {r.attrib["Id"]: r.attrib["Target"] for r in relationships}
    rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    item = next((s for s in book.findall("s:sheets/s:sheet", S) if s.attrib["name"] == sheet), None)
    if item is None:
        raise CorpusError("工作表不存在")
    target = links[item.attrib[rid]].replace("\\", "/")
    target = target.lstrip("/") if target.startswith("/") else "xl/" + target
    xml = ET.fromstring(archive.read(target))
    node = next((c for c in xml.findall(".//s:c", S) if c.attrib["r"] == cell), None)
    if node is None:
        return ""
    if node.attrib.get("t") == "s":
        strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        index = int(node.find("s:v", S).text)
        return "".join(strings[index].itertext())
    if node.attrib.get("t") == "inlineStr":
        return "".join(t.text or "" for t in node.findall(".//s:t", S))
    value = node.find("s:v", S)
    return value.text or "" if value is not None else ""


def verify_document(root: Path, doc: dict[str, Any]) -> dict[str, Any]:
    path = contained_path(root, doc["asset"])
    if not path.is_file() or sha256(path) != doc["sha256"]:
        raise CorpusError(f"文件不存在或哈希不符：{doc['id']}")
    raster_facts = 0
    if doc["format"] == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if len(reader.pages) < (12 if doc["category"] == "long_report" else 3):
            raise CorpusError("PDF页数不足")
        for fact in doc["facts"]:
            page = fact["locator"]["page"]
            if not 1 <= page <= len(reader.pages):
                raise CorpusError("PDF页码越界")
            text = reader.pages[page - 1].extract_text()
            if not text.strip() and doc["category"] == "scan_mixed_pdf":
                if not reader.pages[page - 1].images:
                    raise CorpusError("扫描页缺少栅格图像")
                raster_facts += 1
            elif _compact(fact["quote"]) not in _compact(text):
                raise CorpusError(f"PDF原文来源不匹配：{fact['id']}")
    elif doc["format"] == "docx":
        with zipfile.ZipFile(path) as archive:
            xml = ET.fromstring(archive.read("word/document.xml"))
            body = xml.find("w:body", W)
            paragraphs = body.findall("w:p", W)
            tables = body.findall("w:tbl", W)
            for fact in doc["facts"]:
                locator = fact["locator"]
                if "paragraph" in locator:
                    node = paragraphs[locator["paragraph"] - 1]
                else:
                    table = tables[locator["table"] - 1]
                    node = table.findall("w:tr", W)[locator["row"] - 1].findall("w:tc", W)[locator["column"] - 1]
                text = "".join(t.text or "" for t in node.findall(".//w:t", W))
                if fact["quote"] != text:
                    raise CorpusError(f"DOCX原文来源不匹配：{fact['id']}")
    elif doc["format"] == "xlsx":
        with zipfile.ZipFile(path) as archive:
            cached_formula_count = 0
            formula_count = 0
            for filename in archive.namelist():
                if filename.startswith("xl/worksheets/") and filename.endswith(".xml"):
                    sheet = ET.fromstring(archive.read(filename))
                    for cell in sheet.findall(".//s:c", S):
                        if cell.find("s:f", S) is not None:
                            formula_count += 1
                            if cell.find("s:v", S) is not None:
                                cached_formula_count += 1
            if not formula_count or cached_formula_count:
                raise CorpusError("测试无缓存公式被重算或遗失")
            for fact in doc["facts"]:
                locator = fact["locator"]
                # 所有工作簿的说明原文位于B列；范围同时包含稳定锚点A列。
                row = re.search(r"\d+", locator["range"]).group(0)
                if _cell_value(archive, locator["sheet"], "B" + row) != fact["quote"]:
                    raise CorpusError(f"XLSX原文来源不匹配：{fact['id']}")
    else:
        raise CorpusError("未知格式")
    return {"document_id": doc["id"], "status": "verified", "sha256": doc["sha256"],
        "source_facts": len(doc["facts"]), "raster_source_facts": raster_facts,
        "raster_review": "requires_independent_ocr_or_visual_review" if raster_facts else "not_applicable"}


def prepare_corpus(destination: Path = DEFAULT_CORPUS, fixture_root: Path = FIXTURE_ROOT) -> dict[str, Any]:
    """复制规范资产，不覆盖异内容、不访问正式库；同目录同哈希可安全复用。"""
    destination = Path(destination).resolve()
    fixture_root = Path(fixture_root).resolve()
    if destination == fixture_root or destination.is_relative_to(fixture_root):
        raise CorpusError("输出不能覆盖仓库规范夹具")
    formal_root = Path(__file__).resolve().parents[1] / "data"
    if destination == formal_root.resolve() or any(destination.glob("*.db")) or any(destination.glob("*.sqlite*")):
        raise CorpusError("输出必须使用独立评估目录，不能写入数据库目录")
    manifest = load_manifest(fixture_root)
    # 全部先校验再写入，错误原件不会留下伪成功清单。
    checks = [verify_document(fixture_root, doc) for doc in manifest["documents"]]
    for doc in manifest["documents"]:
        target = contained_path(destination, doc["asset"])
        if target.exists() and sha256(target) != doc["sha256"]:
            raise CorpusError("输出目录含不同内容，拒绝覆盖")
    target_manifest = destination / "manifest.json"
    canonical = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if target_manifest.exists() and target_manifest.read_text(encoding="utf-8") != canonical:
        raise CorpusError("输出目录已有不同评估清单，拒绝覆盖")
    for doc in manifest["documents"]:
        target = contained_path(destination, doc["asset"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(contained_path(fixture_root, doc["asset"]), target)
    target_manifest.write_text(canonical, encoding="utf-8")
    return {"mode": "offline_corpus_preparation", "documents": 18, "scenarios": 144,
        "dev_documents": 12, "holdout_documents": 6, "provider_calls": 0, "verification": checks,
        "semantic_acceptance": "not_executed", "real_world_generalization": "not_executed",
        "annotations": manifest["annotation_status"]}


def evaluate_saved_reviews(manifest: dict[str, Any], reviews: list[dict[str, Any]], *, split: str = "dev") -> dict[str, Any]:
    """复核记录回放，非自动语义judge；没有真实输出与人工核对就保持未执行。"""
    validate_manifest(manifest)
    if split not in {"dev", "holdout", "all"}:
        raise CorpusError("非法评估分组")
    selected = [(d, s) for d in manifest["documents"] if split == "all" or d["split"] == split for s in d["scenarios"]]
    known = {s["id"]: (d, s) for d, s in selected}
    records = {}
    for review in reviews:
        case_id = review.get("scenario_id")
        if case_id not in known or case_id in records:
            raise CorpusError("复核含未知、越组或重复场景")
        if review.get("mode") not in {"saved_response_review", "live_provider_review", "engineering_fixture"}:
            raise CorpusError("必须区分保存响应、真实供应商与工程夹具")
        if not review.get("reviewer") or not review.get("output_sha256") or not re.fullmatch(r"[0-9a-f]{64}", review["output_sha256"]):
            raise CorpusError("复核必须有核对者与真实输出哈希")
        doc, scenario = known[case_id]
        fact_ids = {f["id"] for f in doc["facts"]}
        covered = review.get("supported_fact_ids", [])
        evidence = review.get("input_evidence_fact_ids", [])
        if len(set(covered)) != len(covered) or len(set(evidence)) != len(evidence) or not set(covered + evidence) <= fact_ids:
            raise CorpusError("复核事实未知、重复或跨文档")
        if not set(covered) <= set(evidence):
            raise CorpusError("有据事实必须存在于实际输入证据")
        if not isinstance(review.get("violations"), list) or not isinstance(review.get("fact_support_confirmed"), bool):
            raise CorpusError("必须显式标明事实支持复核与违规项")
        records[case_id] = deepcopy(review)
    rows = []
    for doc, scenario in selected:
        review = records.get(scenario["id"])
        required = set(scenario["required_fact_ids"])
        if review is None:
            rows.append({"scenario_id": scenario["id"], "category": doc["category"], "task": scenario["task"],
                "status": "not_executed", "required": len(required), "covered": 0, "evidence_covered": 0})
            continue
        covered = required & set(review["supported_fact_ids"])
        evidence = required & set(review["input_evidence_fact_ids"])
        passed = (covered == required and review["fact_support_confirmed"] and not review["violations"]
            and review.get("answer_status") == scenario["expected_status"])
        rows.append({"scenario_id": scenario["id"], "category": doc["category"], "task": scenario["task"],
            "status": "passed" if passed else "failed", "mode": review["mode"],
            "required": len(required), "covered": len(covered), "evidence_covered": len(evidence),
            "violations": review["violations"], "fact_support_confirmed": review["fact_support_confirmed"]})
    def totals(items):
        executed = [r for r in items if r["status"] != "not_executed"]
        denominator = sum(r["required"] for r in executed)
        return {"planned": len(items), "executed": len(executed),
            "passed": sum(r["status"] == "passed" for r in items), "failed": sum(r["status"] == "failed" for r in items),
            "not_executed": len(items) - len(executed),
            "required_fact_denominator_executed_only": denominator,
            "supported_fact_numerator": sum(r["covered"] for r in executed),
            "input_evidence_numerator": sum(r["evidence_covered"] for r in executed),
            "supported_fact_coverage": sum(r["covered"] for r in executed) / denominator if denominator else None}
    return {"mode": "saved_review_accounting_only", "split": split, "provider_calls_this_run": 0,
        "semantic_acceptance": "not_automatically_certified", "review_provenance": "caller_supplied_manual_review_records",
        "scope": manifest["scope"], "overall": totals(rows),
        "by_category": {cat: totals([r for r in rows if r["category"] == cat]) for cat in CATEGORIES},
        "by_task": {task: totals([r for r in rows if r["task"] == task]) for task in ("qa", "summary", "extraction")},
        "by_evidence_mode": {mode: totals([r for r in rows if r.get("mode") == mode]) for mode in ("engineering_fixture", "saved_response_review", "live_provider_review")},
        "real_world_generalization": "not_executed", "results": rows}
