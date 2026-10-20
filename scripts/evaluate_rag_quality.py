"""固定黄金集的离线检索评估。无在线模式、无密钥读取；旧报告不覆盖。"""
from __future__ import annotations
import argparse
import runpy
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.rag_context import build_evidence
from app.quote_spans import task_segment_payload as task_evidence_payload
from app.rag_evaluation import EvaluationError, digest, evidence_metrics, admitted_quote_metrics, summarize, retrieval_gate, validate_case
from app.rag_extract_context import admitted_ranges
from app.retrieval_planning import plan_retrieval, _plan_retrieval_v1
from app.schemas import Chunk


def evaluate(cases: list[dict], planner=plan_retrieval):
    rows = []
    for case in cases:
        started = time.perf_counter()
        sources=validate_case(case)
        def verify_hits(hits):
            for hit in hits:
                source=sources.get(hit["chunk_id"])
                if (source is None or hit["text"]!=source["text"] or
                        hit["document_id"]!=source["document_id"] or
                        hit["parse_version_id"]!=source["parse_version_id"]):
                    raise EvaluationError("run_text_or_scope_changed")
        verify_hits(case["vector_result"]["results"])
        chunks = [Chunk(**c) for c in case["chunks"]]
        planned = planner(case["question"], case["vector_result"], chunks,
            fulltext_budget=case["context_max_chars"], max_items=case["context_k"],
            payload_transform=task_evidence_payload)
        verify_hits(planned["results"])
        settings = Settings(rag_retrieval_k=20, rag_context_k=case["context_k"],
                            rag_context_max_chars=case["context_max_chars"])
        built = build_evidence(planned, settings, expected_document_id=case["document_id"],
                               payload_transform=task_evidence_payload,
                               extractive_question=case['question'] if planner is plan_retrieval else None)
        if built.fatal_reason:
            raise EvaluationError("evidence_scope_mismatch")
        candidates = [hit["chunk_id"] for hit in planned["results"]]
        selected = [item.chunk_id for item in built.pack.items]
        if any(item.text!=sources[item.chunk_id]["text"].replace("\r\n","\n") for item in built.pack.items):
            raise EvaluationError("packed_text_changed")
        local_ms=round((time.perf_counter()-started)*1000,3)
        oracle_ids=list(dict.fromkeys(f["chunk_id"] for p in case["required_evidence"]
            for f in p["alternatives"][0]))
        oracle_hits=[{**sources[i],"chunk_id":i,"score":1.0} for i in oracle_ids]
        oracle=build_evidence({"index_id":"offline-oracle","parse_version_id":case["parse_version_id"],
            "results":oracle_hits},settings,expected_document_id=case["document_id"],payload_transform=task_evidence_payload)
        rows.append(dict(case_id=case["case_id"], source_kind=case["source_kind"], category=case["category"],
            case_sha256=digest(case), candidate_ids=candidates, selected_ids=selected,
            retrieval={**evidence_metrics(case,candidates,selected),'input_quote_recall':admitted_quote_metrics(case,
                {i.chunk_id:admitted_ranges(i.text,i.read_ranges,strict=i.segment_policy=='sentence-segments-v1') for i in built.pack.items})},
            context_chars=built.pack.context_chars, context_k=case["context_k"],
            context_max_chars=case["context_max_chars"], drop_reasons=built.pack.drop_reasons,
            planning=planned.get("planning", {}), local_ms=local_ms,
            provider_usage=None,online_latency_ms=None,monetary_cost=None,
            oracle={"required_chunk_ids":oracle_ids,"fits_same_budget":
                set(oracle_ids)=={i.chunk_id for i in oracle.pack.items},"generation_status":"not_executed"}))
    return {"schema": "docqa-rag-quality-v1", "cases_sha256": digest(cases),
            "summary": summarize(rows), "cases": rows}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases",type=Path, help="冻结的JSON场景列表；默认使用CC0合成黄金集")
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--strategy",choices=("current","legacy"),default="current")
    parser.add_argument("--baseline",type=Path,help="可选的同一冻结场景基线报告")
    parser.add_argument("--gate",action="store_true",help="逐题防退步并按类别检查90%%输入召回，失败返回1")
    args=parser.parse_args(argv)
    if args.report.exists():
        parser.error("报告已存在，请使用新路径保留首次结果")
    try:
        cases=(json.loads(args.cases.read_text(encoding="utf-8")) if args.cases else
               runpy.run_path(str(ROOT/'tests/fixtures/rag_quality/golden.py'))["make_cases"]())
        report=evaluate(cases, planner=plan_retrieval if args.strategy=="current" else _plan_retrieval_v1)
        report["strategy"]=args.strategy
        baseline=json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
        report["gate"]=retrieval_gate(report,baseline) if args.gate else None
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    except (EvaluationError,ValueError,KeyError,OSError) as error:
        print(json.dumps({"error":str(error),"provider_calls_this_run":0},ensure_ascii=True))
        return 2
    print(json.dumps({"overall":report["summary"]["overall"],"gate":report["gate"],
                      "report":str(args.report.resolve())},ensure_ascii=True))
    return 1 if report["gate"] and not report["gate"]["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
