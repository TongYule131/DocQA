"""在线语义结果的逐条复核证据：对已保存的原始模型正文做可复核的一致性检查。

本脚本**不发起任何请求**。它读取在线评估保存的原始正文，重新执行生产校验器，
并把每条事实/要点的引用、引述与来源列出来，便于人工逐条核对
（对应任务书 §11.2「逐事实复核」的要求）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analysis_validation import (  # noqa: E402
    AnalysisOutputError,
    validate_extraction_batch,
    validate_summary_batch,
)
from scripts.evaluate_analysis import (  # noqa: E402
    _units_of,
    build_case_plan,
    load_cases,
    sample_settings,
)

EVIDENCE = PROJECT_ROOT / "data" / "stable-release-dev" / "analysis-eval" / "evidence"


def review_case(case_id: str, data_dir: Path) -> dict:
    path = EVIDENCE / f"{case_id}-raw.json"
    if not path.exists():
        return {"case_id": case_id, "status": "no_evidence", "path": str(path)}
    record = json.loads(path.read_text(encoding="utf-8"))
    raw = record[0].get("raw")
    if raw is None:
        return {"case_id": case_id, "status": "no_raw_output",
                "failure": record[0].get("detail"), "path": str(path)}
    case = load_cases([case_id])[0]
    settings = sample_settings(data_dir)
    plan = build_case_plan(case, settings, document_name="评估合成样本",
                           version_id=f"v-eval-{case_id}")
    units = _units_of(plan.batches[0])
    try:
        if case.kind == "extraction":
            validated = validate_extraction_batch(
                raw, units=units, batch_id="b1",
                max_items=settings.analysis_max_items_per_batch)
            facts = [{"kind": item.kind, "content": item.content, "refs": item.refs,
                      "value_text": item.value_text, "unit": item.unit,
                      "period": item.period, "subject": item.subject, "scope": item.scope}
                     for item in validated.items]
        else:
            validated = validate_summary_batch(raw, units=units, batch_id="b1")
            facts = [{"kind": "point", "content": point.text, "refs": point.refs}
                     for point in validated.main_points]
            facts += [{"kind": "exception", "content": point.text, "refs": point.refs}
                      for point in validated.exceptions]
        quotes = {str(ref): quote for ref, quote in validated.quotes.items()}
        original = {unit.batch_local_id: unit.text for unit in units}
        trace = []
        for fact in facts:
            for ref in fact["refs"]:
                quote = quotes.get(str(ref), "")
                unit_text = original.get(ref, "")
                trace.append({
                    "content": fact["content"],
                    "ref": ref,
                    "quote": quote,
                    "traceable": bool(quote) and quote in unit_text,
                })
        return {"case_id": case_id, "status": "validated", "facts": facts,
                "trace": trace, "path": str(path),
                "planned_batches": record[0].get("batch_total"),
                "case_kind": case.kind}
    except AnalysisOutputError as exc:
        return {"case_id": case_id, "status": "validator_rejected", "reason": exc.reason,
                "detail": exc.detail, "path": str(path), "raw": raw}


def main() -> int:
    data_dir = PROJECT_ROOT / "data" / "stable-release-dev" / "analysis-eval"
    outputs = {}
    for case_id in ("E1", "E2", "E3", "E4", "S1", "S2", "S3", "S4"):
        result = review_case(case_id, data_dir)
        outputs[case_id] = result
        status = result["status"]
        print(f"{case_id}: {status}")
        if status == "validated":
            planned = result.get("planned_batches")
            print(f"    规划批次数：{planned}"
                  + ("（需要 1 次汇总调用）" if planned and planned > 1 else "（单批，无汇总调用）"))
            for fact in result["facts"]:
                marker = "".join(f"[{ref}]" for ref in fact["refs"])
                print(f"    - ({fact['kind']}) {fact['content']} {marker}")
            bad = [item for item in result["trace"] if not item["traceable"]]
            print(f"    引述可追溯：{len(result['trace']) - len(bad)}/{len(result['trace'])}")
        elif status == "validator_rejected":
            print(f"    reason={result['reason']} detail={result['detail']}")
        elif status == "no_raw_output":
            print(f"    未保存原始正文；此前失败原因：{result['failure']}")
    out = EVIDENCE / "manual-review.json"
    out.write_text(json.dumps(outputs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n逐条复核证据：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
