"""默认离线的RAG度量：联合证据召回与人工复核分开，缺测不计通过。

黄金标签描述必要证据组；它不把词面命中当作生成正确。原文及人工判断均绑定
哈希，不能拿另一版答案的复核记录给新答案背书。该模块不读取配置或访问网络。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict


class EvaluationError(ValueError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def fraction(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def validate_case(case: dict) -> dict[str, dict]:
    chunks = {c["id"]: c for c in case["chunks"]}
    if len(chunks) != len(case["chunks"]) or not case["case_id"]:
        raise EvaluationError("duplicate_chunk_or_empty_case")
    for chunk in chunks.values():
        if (chunk["document_id"] != case["document_id"] or
                chunk["parse_version_id"] != case["parse_version_id"]):
            raise EvaluationError("source_scope_mismatch")
    points = set()
    for point in case["required_evidence"]:
        if point["point_id"] in points or not point["alternatives"]:
            raise EvaluationError("invalid_evidence_point")
        points.add(point["point_id"])
        for group in point["alternatives"]:
            if not group:
                raise EvaluationError("empty_evidence_group")
            for fragment in group:
                chunk = chunks.get(fragment["chunk_id"])
                if not chunk or not fragment["quote"] or fragment["quote"] not in chunk["text"]:
                    raise EvaluationError("gold_quote_not_in_source")
    if not set(case.get("relevant_chunk_ids", [])) <= set(chunks):
        raise EvaluationError("unknown_relevance_label")
    return chunks


def evidence_metrics(case: dict, candidate_ids: list[str], selected_ids: list[str]) -> dict:
    chunks = validate_case(case)
    if any(i not in chunks for i in candidate_ids + selected_ids):
        raise EvaluationError("run_source_mismatch")
    if len(set(selected_ids)) != len(selected_ids) or not set(selected_ids) <= set(candidate_ids):
        raise EvaluationError("invalid_selected_evidence")
    points = case["required_evidence"]
    def coverage(ids):
        found = {i for i in ids}
        hit = [p["point_id"] for p in points if any(
            all(f["chunk_id"] in found for f in group) for group in p["alternatives"])]
        return {**fraction(len(hit), len(points)), "missing_points": [
            p["point_id"] for p in points if p["point_id"] not in hit]}
    def precision(ids):
        if case.get("relevance_exhaustive") is not True:
            return {"value": None, "status": "relevance_not_exhaustively_labelled"}
        relevant = set(case.get("relevant_chunk_ids", []))
        return fraction(sum(i in relevant for i in ids), len(ids))
    return {"candidate_recall": coverage(candidate_ids), "input_recall": coverage(selected_ids),
            "candidate_precision": precision(list(dict.fromkeys(candidate_ids))),
            "input_precision": precision(selected_ids),
            "answerability": "answerable" if case["expected_answerable"] else "unanswerable",
            "generated_answer_quality": "not_evaluated_by_retrieval_metrics"}


def reviewed_answer_metrics(case: dict, answer: dict | None, review: dict | None) -> dict:
    if answer is None or review is None:
        return {"status": "unverified", "faithfulness": None, "answer_relevance": None,
                "answer_coverage": None, "citation_support": None}
    if review.get("case_id") != case["case_id"] or review.get("answer_sha256") != digest(answer):
        raise EvaluationError("review_answer_mismatch")
    if review.get("case_sha256") != digest(case):
        raise EvaluationError("review_source_mismatch")
    if not isinstance(review.get("reviewer"),str) or not review["reviewer"].strip():
        raise EvaluationError("reviewer_required")
    facts = [*answer.get("conclusion", []), *answer.get("explanation", [])]
    checks = review.get("fact_checks", [])
    if len(checks) != len(facts) or [c.get("index") for c in checks] != list(range(len(facts))):
        raise EvaluationError("incomplete_fact_review")
    fields = ("supported", "relevant", "citation_supported", "conditions_complete")
    if any(type(c.get(f)) is not bool for c in checks for f in fields):
        raise EvaluationError("invalid_review_judgement")
    required = {p["point_id"] for p in case["required_evidence"]}
    covered = review.get("covered_points", [])
    if len(set(covered)) != len(covered) or not set(covered) <= required:
        raise EvaluationError("invalid_review_points")
    if type(review.get("status_appropriate")) is not bool:
        raise EvaluationError("invalid_status_review")
    return {"status": "reviewed", "reviewer": review.get("reviewer"),
            "independent": review.get("independent") is True,
            "faithfulness": fraction(sum(c["supported"] for c in checks),len(facts)),
            "answer_relevance": fraction(sum(c["relevant"] for c in checks),len(facts)),
            "citation_support": fraction(sum(c["citation_supported"] for c in checks),len(facts)),
            "conditions_complete": fraction(sum(c["conditions_complete"] for c in checks),len(facts)),
            "answer_coverage": fraction(len(covered), len(required)),
            "status_appropriate": review["status_appropriate"],
            "severe_errors": review.get("severe_errors", [])}


def summarize(rows: list[dict]) -> dict:
    def aggregate(group):
        metrics = {}
        for name in ("candidate_recall", "input_recall", "candidate_precision", "input_precision"):
            known = [r["retrieval"][name] for r in group if "numerator" in r["retrieval"][name]]
            metrics[name] = {**fraction(sum(m["numerator"] for m in known),
                                      sum(m["denominator"] for m in known)),
                             "labelled_cases": len(known)}
        durations = sorted(r["local_ms"] for r in group)
        metrics.update(case_count=len(group), generated_answers_this_run=0, provider_calls_this_run=0,
                       online_generation_status="not_executed", local_p50_ms=durations[(len(durations)-1)//2] if durations else None,
                       local_p95_ms=durations[max(0, math.ceil(len(durations)*0.95)-1)] if durations else None,
                       generated_semantic_passes=None)
        return metrics
    groups = defaultdict(list)
    for row in rows:
        groups[(row["source_kind"], row["category"])].append(row)
    return {"overall": aggregate(rows), "by_category": [
        {"source_kind": key[0], "category": key[1], **aggregate(group)} for key,group in sorted(groups.items())]}


def retrieval_gate(report: dict, baseline: dict | None = None, *, min_recall: float = 0.9) -> dict:
    """仅检索质量关卡：按题防退步，缺测不能变成发布通过。"""
    if not 0 <= min_recall <= 1 or not report.get("cases"):
        raise EvaluationError("invalid_gate_input")
    failures=[]
    current={r["case_id"]:r for r in report["cases"]}
    if len(current)!=len(report["cases"]):
        raise EvaluationError("duplicate_case_result")
    if baseline is not None:
        previous={r["case_id"]:r for r in baseline["cases"]}
        if (report["cases_sha256"]!=baseline["cases_sha256"] or set(current)!=set(previous) or
                any(r["case_sha256"]!=previous[k]["case_sha256"] for k,r in current.items())):
            raise EvaluationError("baseline_case_mismatch")
        for key,row in current.items():
            for name in ("candidate_recall","input_recall"):
                now=row["retrieval"][name]["value"]
                before=previous[key]["retrieval"][name]["value"]
                if before is not None and (now is None or now+1e-12<before):
                    failures.append({"case_id":key,"metric":name,"reason":"regression"})
    for group in report["summary"]["by_category"]:
        recall=group["input_recall"]["value"]
        if recall is not None and recall<min_recall:
            failures.append({"category":group["category"],"reason":"input_recall_below_target"})
    return {"scope":"offline_retrieval_only","passed":not failures,"failures":failures,
            "min_input_recall":min_recall,"online_semantic_release":"unverified"}
