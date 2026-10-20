"""隔离OCR实验的离线度量；不读配置/密钥、不访问模型，不修改原文。

字符识别、版面顺序和表格归属必须分别报告。缺失输出按全部漏字处理，
不能只在成功区域上算平均值；关键片段检出不等于逐字段精确率。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import statistics
from typing import Any


def text_characters(text: str) -> str:
    """只去空白，不合并全半角、引号、小数点或负号。"""
    return re.sub(r"\s+", "", text)


def edit_counts(expected: str, actual: str) -> dict[str, int | float | None]:
    # 动态规划固定同成本优先级：匹配、替换、删除、插入。保留三类错误供复核。
    ref, hyp = text_characters(expected), text_characters(actual)
    previous = [(i, 0, 0, i) for i in range(len(hyp) + 1)]
    for i, a in enumerate(ref, 1):
        current = [(i, 0, i, 0)]
        for j, b in enumerate(hyp, 1):
            if a == b:
                current.append(previous[j - 1])
                continue
            old = previous[j - 1]
            replace = (old[0] + 1, old[1] + 1, old[2], old[3])
            old = previous[j]
            delete = (old[0] + 1, old[1], old[2] + 1, old[3])
            old = current[j - 1]
            insert = (old[0] + 1, old[1], old[2], old[3] + 1)
            current.append(min((replace, delete, insert), key=lambda v: v[0]))
        previous = current
    edits, substitutions, deletions, insertions = previous[-1]
    return {"reference_characters": len(ref), "output_characters": len(hyp), "edits": edits,
            "substitutions": substitutions, "deletions": deletions, "insertions": insertions,
            "cer": edits / len(ref) if ref else None, "exact": ref == hyp}


def contains_critical_span(actual: str, span: str) -> bool:
    value, wanted = actual, text_characters(span)
    if not wanted:
        raise ValueError("关键片段不能为空")
    # 25%不能命中125%或-25%；1.3不能命中1.30或11.3。中文主体允许紧邻数字。
    left = r"(?<![\d.\-+])" if wanted[0].isdigit() or wanted[0] in "+-" else ""
    right = r"(?![\d.%％])" if wanted[-1].isdigit() or wanted[-1] in "%％" else ""
    # 保留输出空白作为数值边界，避免把表格相邻的2277.9与1.3拼成2277.91.3。
    pattern = r"\s*".join(re.escape(char) for char in wanted)
    return re.search(left + pattern + right, value) is not None


def load_corpus(directory: Path) -> dict[str, Any]:
    root = directory.resolve()
    manifest = json.loads((root / "corpus.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "docqa-ocr-corpus-v1" or not manifest.get("frozen"):
        raise ValueError("需要已冻结的OCR标注")
    identifiers, families = set(), {}
    if not manifest.get("regions"):
        raise ValueError("标注区域不能为空")
    for row in manifest["regions"]:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", row["id"]) or row["id"] in identifiers or row["split"] not in {"dev", "holdout"}:
            raise ValueError("重复区域或非法分组")
        identifiers.add(row["id"])
        if not row.get("reviewed") or not row.get("expected") or row["category"] not in {"normal", "difficult"}:
            raise ValueError("标注未复核或内容无效")
        family = row["document_id"]
        if family in families and families[family] != row["split"]:
            raise ValueError("同源文档跨开发与保留集")
        families[family] = row["split"]
        image = (root / row["image"]).resolve()
        if not image.is_relative_to(root) or hashlib.sha256(image.read_bytes()).hexdigest() != row["image_sha256"]:
            raise ValueError("图像越界或内容改变")
        if any(not contains_critical_span(row["expected"], span) for span in row["critical_spans"]):
            raise ValueError(f"关键片段不在标注区域: {row['id']}")
    return manifest


def score_predictions(manifest: dict[str, Any], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {p["id"]: p for p in predictions}
    ids = {r["id"] for r in manifest["regions"]}
    if len(by_id) != len(predictions) or not by_id.keys() <= ids:
        raise ValueError("输出包含重复或未知区域")
    rows = []
    for region in manifest["regions"]:
        output = by_id.get(region["id"], {})
        error = output.get("error") or ("missing_output" if not output else None)
        actual = output.get("text", "")
        if not isinstance(actual, str):
            raise ValueError("OCR文字必须为字符串")
        if error:
            actual = ""  # 失败结果不作为已交付文字，原始响应仍由运行器保留。
        metrics = edit_counts(region["expected"], actual)
        critical = [{"span": span, "found": contains_critical_span(actual, span)} for span in region["critical_spans"]]
        rows.append({"id": region["id"], "document_id": region["document_id"], "split": region["split"],
                     "category": region["category"], "source_kind": region["source_kind"], **metrics,
                     "critical": critical, "critical_total": len(critical),
                     "critical_found": sum(v["found"] for v in critical), "error": error})
    def aggregate(items):
        count = sum(r["reference_characters"] for r in items)
        critical = sum(r["critical_total"] for r in items)
        return {"regions": len(items), "reference_characters": count,
                "edits": sum(r["edits"] for r in items),
                "cer": sum(r["edits"] for r in items) / count if count else None,
                "exact_regions": sum(r["exact"] and not r["error"] for r in items),
                "critical_found": sum(r["critical_found"] for r in items), "critical_total": critical,
                "critical_recall": sum(r["critical_found"] for r in items) / critical if critical else None,
                "failures": sum(bool(r["error"]) for r in items)}
    return {"schema_version": "docqa-ocr-score-v1", "overall": aggregate(rows),
            "by_split": {k: aggregate([r for r in rows if r["split"] == k]) for k in ("dev", "holdout")},
            "by_category": {k: aggregate([r for r in rows if r["category"] == k]) for k in ("normal", "difficult")},
            "by_source_kind": {k: aggregate([r for r in rows if r["source_kind"] == k]) for k in sorted({r["source_kind"] for r in rows})},
            "reading_order": {"status": "not_evaluated"}, "table_structure": {"status": "not_evaluated"},
            "regions": rows}


def promotion_gate(baseline: dict, candidate: dict, baseline_run: dict, candidate_run: dict) -> dict:
    """冻结标准v1的保留集切换关卡；不把少量平均错字下降当作全部通过。"""
    if baseline_run.get('split')!='holdout' or candidate_run.get('split')!='holdout':
        raise ValueError('切换结论必须基于保留集')
    if baseline_run['corpus_sha256']!=candidate_run['corpus_sha256']:
        raise ValueError('两方案不是同一冻结标注')
    if {r['id'] for r in baseline['regions']}!={r['id'] for r in candidate['regions']}:
        raise ValueError('区域范围不同')
    def missing(report):
        return {(r['id'],item['span']) for r in report['regions'] for item in r['critical'] if not item['found']}
    newly_missing=sorted(missing(candidate)-missing(baseline))
    base_cer,cer=baseline['overall']['cer'],candidate['overall']['cer']
    relative=(base_cer-cer)/base_cer if base_cer else (0 if not cer else None)
    base_time=statistics.median(p['elapsed_seconds'] for p in baseline_run['predictions'])
    candidate_time=statistics.median(p['elapsed_seconds'] for p in candidate_run['predictions'])
    ratio=candidate_time/base_time
    checks={
        'normal_cer_le_2pct':candidate['by_category']['normal']['cer'] is not None and candidate['by_category']['normal']['cer']<=.02,
        'difficult_cer_le_5pct':candidate['by_category']['difficult']['cer'] is not None and candidate['by_category']['difficult']['cer']<=.05,
        'critical_recall_ge_99pct':candidate['overall']['critical_recall'] is not None and candidate['overall']['critical_recall']>=.99,
        'no_new_critical_errors':not newly_missing,
        'cer_relative_improvement_ge_20pct':relative is not None and (relative>=.2 if base_cer else cer==0),
        'gpu_device_peak_le_6_5gib':candidate_run['gpu']['peak_bytes']<=6.5*1024**3,
        'median_latency_le_2x':ratio<=2,
        'no_failures':candidate['overall']['failures']==0 and bool(candidate_run.get('completed')),
    }
    return {'standard_version':'docqa-ocr-standard-v1','accepted_for_default':all(checks.values()),
            'checks':checks,'new_missing_critical':[{'id':a,'span':b} for a,b in newly_missing],
            'relative_cer_improvement':relative,'median_latency_ratio':ratio,
            'scope':'visible_holdout_region_character_recognition_only',
            'not_covered':['full_page_reading_order','table_structure','diagram_semantics','independent_blind_test']}
