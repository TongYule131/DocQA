"""只准备下一轮真实复验计划及请求预览；无在线执行和批准写入能力。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.quote_spans import task_segment_payload as task_evidence_payload
from app.rag_context import build_evidence
from app.rag_evaluation import EvaluationError,digest,validate_case
from app.rag_prompts import TASK_PROMPT_VERSION,build_task_system_prompt,build_task_user_prompt
from app.retrieval_planning import _plan_retrieval_v1,plan_retrieval,RETRIEVAL_STRATEGY_VERSION
from app.schemas import Chunk


def prepare(cases: list[dict],target: Path):
    if target.exists():
        raise EvaluationError("plan_directory_exists")
    if not cases or len({c["case_id"] for c in cases})!=len(cases):
        raise EvaluationError("invalid_case_list")
    previews=[]
    for case in cases:
        sources=validate_case(case)
        chunks=[Chunk(**c) for c in case["chunks"]]
        settings=Settings(rag_retrieval_k=20,rag_context_k=case["context_k"],
                          rag_context_max_chars=case["context_max_chars"])
        oracle_ids=list(dict.fromkeys(f["chunk_id"] for p in case["required_evidence"]
            for f in p["alternatives"][0]))
        for chunk_id in case.get("oracle_context_chunk_ids",[]):
            if chunk_id not in sources:
                raise EvaluationError("unknown_oracle_context")
            if chunk_id not in oracle_ids:
                oracle_ids.append(chunk_id)
        oracle={"index_id":"offline-oracle","parse_version_id":case["parse_version_id"],
                "results":[{**sources[i],"chunk_id":i,"score":1.0} for i in oracle_ids]}
        for branch,planner in (("legacy",_plan_retrieval_v1),("current",plan_retrieval),("oracle",None)):
            retrieved=planner(case["question"],case["vector_result"],chunks,
                fulltext_budget=case["context_max_chars"],max_items=case["context_k"],
                payload_transform=task_evidence_payload) if planner else oracle
            for hit in retrieved["results"]:
                source=sources.get(hit["chunk_id"])
                if (source is None or hit["text"]!=source["text"] or
                        hit["document_id"]!=source["document_id"] or
                        hit["parse_version_id"]!=source["parse_version_id"]):
                    raise EvaluationError("preview_text_or_scope_changed")
            built=build_evidence(retrieved,settings,expected_document_id=case["document_id"],
                payload_transform=task_evidence_payload)
            if built.fatal_reason:
                raise EvaluationError("preview_scope_mismatch")
            if branch=="oracle" and {i.chunk_id for i in built.pack.items}!=set(oracle_ids):
                raise EvaluationError("complete_evidence_exceeds_same_budget")
            # 清单只有冻结分块，尚无可信Block表结构；预览不伪造辅助证据。
            system=build_task_system_prompt(include_table_structure=False)
            user=build_task_user_prompt(question=case["question"],document_name="",evidence_payload=built.pack.payload())
            if len(system)+len(user)>settings.rag_input_max_chars:
                raise EvaluationError("preview_input_exceeds_budget")
            previews.append({"case_id":case["case_id"],"branch":branch,"system":system,
                "user":json.loads(user),"selected_chunk_ids":[i.chunk_id for i in built.pack.items],
                "input_chars":len(system)+len(user),"preview_only":True,
                "table_structure_preview":"unavailable_without_frozen_blocks",
                "note":"真实执行先获取同题共享的实际查询向量；最终输入另行冻结，不覆盖预览。"})
    queries=len(cases)
    generations=len(previews)
    plan={"schema":"docqa-rag-live-plan-v1","approval_status":"not_approved",
          "provider_calls_this_run":0,"cases_sha256":digest(cases),
          "task_prompt_version":TASK_PROMPT_VERSION,"retrieval_version":RETRIEVAL_STRATEGY_VERSION,
          "system_prompt_sha256":digest(build_task_system_prompt()),
          "case_count":len(cases),"document_count":len({c["document_id"] for c in cases}),
          "branches":["legacy","current","oracle"],"same_model_prompt_and_context_budgets":True,
          "planned_requests":{"index":0,"query":queries,"generation":generations,"total":queries+generations},
          "proposed_total_limit":40,"repair_reserve":40-queries-generations,
          "index_policy":"复用已核对的兼容索引；不兼容则先停止，不自动建索引扩大费用。",
          "query_policy":"每题真实查询一次，三分支共享向量；不把缓存时长当完整查询延迟。",
          "stop_policy":"首次工程／上游失败立即停止；保留请求、失败和不确定调用，不自动重试。",
          "review_policy":"逐事实检查来源支持、数值用途及条件、回答覆盖、partial合理性；形式通过单列。",
          "cases":[{"case_id":c["case_id"],"question":c["question"],
                    "document_id":c["document_id"],"parse_version_id":c["parse_version_id"],
                    "source_sha256":digest(c["chunks"]),"required_evidence":c["required_evidence"],
                    "oracle_context_chunk_ids":c.get("oracle_context_chunk_ids",[])} for c in cases]}
    if plan["repair_reserve"]<0:
        raise EvaluationError("plan_exceeds_proposed_limit")
    target.mkdir(parents=True)
    for n,preview in enumerate(previews,1):
        (target/f'request-preview-{n:02d}.json').write_text(json.dumps(preview,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (target/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return plan


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        plan=prepare(json.loads(args.cases.read_text(encoding='utf-8')),args.output_dir)
    except (EvaluationError,ValueError,KeyError,OSError) as error:
        print(json.dumps({'status':'stopped','error_type':type(error).__name__,'provider_calls_this_run':0}))
        return 2
    print(json.dumps({k:plan[k] for k in ['approval_status','provider_calls_this_run','planned_requests','proposed_total_limit']}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
