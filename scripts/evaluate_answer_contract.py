"""默认离线的事实限定协议回放；不读取模型配置，不将协议拦截率称为答案准确率。"""
from pathlib import Path
import argparse
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.rag_answer_contract import validate_task_output
from app.rag_context import EvidenceItem,EvidencePack
from app.rag_evaluation import digest
from app.rag_validation import ModelOutputError
from tests.fixtures.rag_quality.answer_contract import make_cases,semantic_probes


def pack_for(case):
    return EvidencePack(items=[EvidenceItem(reference_id=1,chunk_id="c1",document_id=case["document_id"],
        parse_version_id="v1",page=None,text=case["source"])],candidates=[],index_id="synthetic",
        parse_version_id="v1",truncated=False,context_chars=len(case["source"]))


def evaluate(cases=None,probes=None):
    cases=make_cases() if cases is None else cases
    probes=semantic_probes() if probes is None else probes
    rows=[]
    for case in cases:
        row={"case_id":case["case_id"],"split":case["split"],"source_sha256":digest(case["source"])}
        for name in ("positive","negative"):
            try:
                validate_task_output(json.dumps(case[name],ensure_ascii=False),pack_for(case),question=case["question"])
                row[name]="accepted"
            except ModelOutputError as exc:
                row[name]=exc.reason
        row["guard_passed"]=row["positive"]=="accepted" and row["negative"]!="accepted"
        rows.append(row)
    escaped=[]
    for case in probes:
        try:
            validate_task_output(json.dumps(case["negative"],ensure_ascii=False),pack_for(case),question=case["question"])
            accepted=True
        except ModelOutputError:
            accepted=False
        escaped.append({"case_id":case["case_id"],"structure_accepted":accepted,
                        "semantic_expected":"fail_omitted_qualification","requires_semantic_review":True})
    return {"schema":"docqa-answer-contract-replay-v1","contract_version":"fact-context-parts-v3","cases_sha256":digest(cases),
        "probes_sha256":digest(probes),"provider_calls":0,"model_generation":"not_executed",
        "scope":"8 CC0 synthetic source texts; protocol replay, not independent/blind semantic accuracy",
        "guard_passed":all(r["guard_passed"] for r in rows),"cases":rows,"semantic_probes":escaped,
        "semantic_accuracy":None}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report",type=Path,required=True)
    args=p.parse_args(argv)
    if args.report.exists():p.error("保留首次报告，请使用新路径")
    report=evaluate()
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"pairs":len(report["cases"]),"guard_passed":report["guard_passed"],"semantic_accuracy":None,"provider_calls":0}))
    return 0 if report["guard_passed"] else 1


if __name__=="__main__":raise SystemExit(main())
