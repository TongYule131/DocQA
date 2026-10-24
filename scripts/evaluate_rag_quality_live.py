"""执行已批准的三分支真实对照；复用索引、共享查询、逐HTTP累计计费。"""
from __future__ import annotations

import argparse
from contextvars import ContextVar
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.deepseek import DeepSeekModel
from app.embedding import APIEmbedding
from app.evaluation_budget import BudgetTransport, EvaluationBudget, EvaluationStopped
from app.question_jobs import QuestionJobService
from app.question_worker import QuestionWorker
from app.quote_spans import task_segment_payload as task_evidence_payload
from app.rag import RagService
from app.rag_context import build_evidence
from app.rag_evaluation import digest, evidence_metrics, validate_case
from app.rag_prompts import TASK_PROMPT_VERSION, build_task_system_prompt, build_task_user_prompt
from app.rag_validation import validate_model_output, validate_user_condition_boundary
from app.repository import Repository
from app.retrieval_planning import RETRIEVAL_STRATEGY_VERSION, _plan_retrieval_v1, plan_retrieval
from app.runtime import code_fingerprint
from app.vector_index import DocumentIndex


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_scope(directory, settings, *, repair_revision=None):
    directory = directory.resolve()
    allowed, formal = (ROOT / "data").resolve(), settings.data_dir.resolve()
    if (not directory.is_relative_to(allowed) or directory in {allowed, formal}
            or formal.is_relative_to(directory)):
        raise EvaluationStopped("需要data下独立验收目录")
    approval, plan, cases = (read(directory / name) for name in ("approval.json", "plan.json", "cases.json"))
    if (approval.get("authorized") is not True or approval.get("total_request_limit") != 40
            or approval.get("plan_sha256") != digest(plan) or plan.get("cases_sha256") != digest(cases)
            or plan.get("schema") != "docqa-rag-live-plan-v1"
            or plan.get("branches") != ["legacy", "current", "oracle"]
            or len(cases) != 7 or len({c["case_id"] for c in cases}) != 7
            or len({c["document_id"] for c in cases}) != 3
            or plan.get("planned_requests") != {"index": 0, "query": 7, "generation": 21, "total": 28}):
        raise EvaluationStopped("批准记录、预标注或冻结范围不一致")
    prompt_changed = (plan["task_prompt_version"] != TASK_PROMPT_VERSION
                      or plan["system_prompt_sha256"] != digest(build_task_system_prompt()))
    if plan["retrieval_version"] != RETRIEVAL_STRATEGY_VERSION or (not repair_revision and prompt_changed):
        raise EvaluationStopped("提示词或检索版本已偏离批准计划")
    for case in cases:
        validate_case(case)
    return directory, approval, plan, cases


def freeze_run(directory, frozen, repair_revision=None):
    """修复只能继承原账本身份；新源码记录不能生成新额度或抹去停止记录。"""
    original_path = directory / "runtime-freeze.json"
    stage, fingerprint = directory, digest(frozen)
    freeze_path = original_path
    if repair_revision:
        expected_prompt = "rag-task-v6" if repair_revision == "fact-contract-v6" else "rag-task-v5"
        if repair_revision not in {"prompt-v5", "source-title-spacing", "fact-contract-v6"} or TASK_PROMPT_VERSION != expected_prompt:
            raise EvaluationStopped("没有该修订的已记录修复范围")
        original = read(original_path)
        repair = read(directory / ("repair-" + repair_revision + ".json"))
        if (repair.get("revision") != repair_revision or repair.get("same_stage_limit") != 40
                or repair.get("no_ledger_reset") is not True
                or repair.get("original_freeze_sha256") != digest(original)
                or not read(directory / "state.json").get("stopped")
                or not (directory / "request-ledger.db").is_file()):
            raise EvaluationStopped("缺少原停止、账本或修复依据")
        for key in ("approval_sha256", "plan_sha256", "cases_sha256", "config", "sources"):
            if frozen[key] != original[key]:
                raise EvaluationStopped("修复不得改变批准、题目、来源或模型配置")
        if repair_revision == "source-title-spacing":
            parent = directory / "revisions" / "prompt-v5"
            parent_freeze = read(parent / "runtime-freeze.json")
            if (repair.get("parent_stage") != "revisions/prompt-v5"
                    or repair.get("reuse_successful_from") != "revisions/prompt-v5"
                    or repair.get("parent_freeze_sha256") != digest(parent_freeze)
                    or parent_freeze["system_prompt_sha256"] != digest(build_task_system_prompt())
                    or not read(parent / "state.json").get("stopped")):
                raise EvaluationStopped("前次修订未停止或生成规则已变化，不能复用回答")
        if repair_revision == "fact-contract-v6":
            expected_targets=["xlsx-revenue:Q2","xlsx-revenue:Q4","docx-learning:Q4","paper:Q3"]
            parent=directory/"revisions"/"source-title-spacing"
            if (repair.get("parent_stage")!="revisions/source-title-spacing"
                    or repair.get("targets")!=expected_targets or repair.get("branch")!="current"
                    or repair.get("maximum_new_generations")!=4
                    or repair.get("parent_freeze_sha256")!=digest(read(parent/"runtime-freeze.json"))
                    or repair.get("parent_semantic_sha256")!=digest(read(directory/"semantic-review-final.json"))
                    or read(directory/"semantic-review-final.json").get("overall")!="not_fully_accepted"):
                raise EvaluationStopped("定向复验须保持四个既有问题、相同账本和已记录的语义差距")
        if frozen["code_fingerprint"] == original["code_fingerprint"]:
            raise EvaluationStopped("没有实质代码修复，不重复执行")
        fingerprint = digest(original)
        stage = directory / "revisions" / repair_revision
        stage.mkdir(parents=True, exist_ok=True)
        freeze_path = stage / "runtime-freeze.json"
        frozen = {**frozen, "parent_budget_fingerprint": fingerprint,
                  "repair_sha256": digest(repair), "task_prompt_version": TASK_PROMPT_VERSION,
                  "system_prompt_sha256": digest(build_task_system_prompt())}
    if freeze_path.exists() and read(freeze_path) != frozen:
        raise EvaluationStopped("代码或冻结配置变化，不能自动继续")
    if not freeze_path.exists():
        save(freeze_path, frozen)
    return stage, fingerprint


def revalidate_saved_answer(record, request, raw, pack, *, case, snapshot, prior_snapshot):
    """仅复用相同输入/提示词的成功生成；重新走当前严格解析和来源边界。"""
    from app.rag_composition import needs_plan,refine_pack,writer_prompt
    current=snapshot.get('answer_contract_version')=='fact-context-parts-v8'
    if current:
        if needs_plan(case['question']):raise EvaluationStopped('缺少复杂题原始规划检查点，不复用')
        pack=refine_pack(pack)
    expected_request = {"system": build_task_system_prompt(
        include_table_structure=any(item.table_projection_included for item in pack.items),question=case['question'] if current else None), "user": json.loads(build_task_user_prompt(
        question=case["question"], document_name=snapshot["filename"], evidence_payload=pack.payload()))}
    if current:expected_request['user']=json.loads(writer_prompt(json.dumps(expected_request['user'],ensure_ascii=False),pack))
    if (record.get("case_sha256") != digest(case) or record["job"]["status"] != "succeeded"
            or request != expected_request or prior_snapshot != snapshot
            or record["selected_chunk_ids"] != [i.chunk_id for i in pack.items]):
        raise EvaluationStopped("旧回答输入、范围或状态变化，不能复用")
    from app.rag_answer_contract import validate_task_output
    validated = (validate_task_output(raw,pack,question=case["question"],
                 contract_version=snapshot.get("answer_contract_version", "fact-context-parts-v3")) if "question_parts" in request["user"]
                 else validate_model_output(raw, pack, task_protocol=True))
    validate_user_condition_boundary(validated, pack, question=case["question"])
    from app.rag_support_review import review_claims
    review_claims(validated,pack)
    answer = record["result"]["answer"]
    if current and validated.presentation!=answer.get('presentation'):
        raise EvaluationStopped('旧回答的段落编排与原始响应不一致')
    def facts(rows):
        return [{"text": f.text, "refs": sorted(f.refs), "context": f.context, "claim_type": f.claim_type,
                 'support_review':f.support_review} for f in rows]
    def previous_facts(rows):
        return [{"text": f["text"], "refs": f["refs"], "context": f.get("context", []),
                 "claim_type": f.get("claim_type", "unclassified"),'support_review':f.get('support_review',{})} for f in rows]
    if (validated.status != answer["status"] or facts(validated.conclusion) != previous_facts(answer["conclusion"])
            or facts(validated.explanation) != previous_facts(answer["explanation"])
            or validated.missing_points != answer["missing_points"]
            or validated.clarification_questions != answer["clarification_questions"]
            or validated.quotes != {c["reference_id"]: c["quote"] for c in answer["citations"]}):
        raise EvaluationStopped("旧原始输出与发布结果不一致，不能复用")
    if validated.citation_bindings:
        from app.rag_citation_identity import citation_identity
        citations = {c["reference_id"]: c for c in answer["citations"]}
        identities = {}
        for ref, binding in validated.citation_bindings.items():
            item = pack.item_by_ref(binding["source_ref"])
            expected = citation_identity(item.document_id, item.parse_version_id, item.chunk_id,
                binding["char_start"], binding["char_end"], validated.quotes[ref])
            if (citations[ref].get("source_reference_id") != binding["source_ref"]
                    or any(citations[ref].get(k) != v for k,v in expected.items())):
                raise EvaluationStopped("旧回答的证据身份与原始选择不一致")
            identities[ref] = expected["evidence_id"]
        if any(f.get("evidence_ids") != [identities[ref] for ref in f["refs"]]
               for f in answer["conclusion"] + answer["explanation"]):
            raise EvaluationStopped("旧回答的逐事实证据绑定不一致")


def branch_retrieval(case, branch, raw, chunks, context_cap):
    if branch in {"legacy", "current"}:
        planner = _plan_retrieval_v1 if branch == "legacy" else plan_retrieval
        return planner(case["question"], raw, chunks, fulltext_budget=context_cap,
                       max_items=case["context_k"], payload_transform=task_evidence_payload)
    if branch != "oracle":
        raise EvaluationStopped("未知分支")
    identifiers = list(dict.fromkeys(f["chunk_id"] for point in case["required_evidence"]
                                   for f in point["alternatives"][0]))
    identifiers = list(dict.fromkeys(identifiers + case.get("oracle_context_chunk_ids", [])))
    sources = {c.id: c for c in chunks}
    # oracle仅用于诊断生成能力，明确标记；真实余弦分值仍用于相同的最低分过滤。
    hits = [{**sources[i].model_dump(), "chunk_id": i, "score": raw["candidate_scores"][i]}
            for i in identifiers]
    return {**raw, "results": hits, "strategy": "hybrid-context-v2",
            "evidence_order": identifiers, "evaluation_branch": "oracle",
            "planning": {"route": "manually_complete_evidence"}}


def run(directory, *, allow_online=False, repair_revision=None):
    env = Settings.from_env()
    directory, approval, plan, cases = load_scope(directory, env, repair_revision=repair_revision)
    if not allow_online:
        planned = 21 + sum(not (directory / "queries" / (c["case_id"].replace(":", "-") + ".json")).is_file()
                           for c in cases) if repair_revision else 28
        if repair_revision == "source-title-spacing":
            prior = read(directory / "revisions" / "prompt-v5" / "state.json")
            planned -= sum(a["status"] == "succeeded" for a in prior["answers"].values())
        if repair_revision == "fact-contract-v6":
            planned=4
        return {"mode": "plan_only", "provider_calls_this_run": 0, "planned": planned, "limit": 40}
    settings = replace(env, data_dir=directory / "run-data", allow_database_upgrade=False,
                       rag_retrieval_k=20, rag_context_k=20, question_context_k=20,
                       question_retrieval_strategy="hybrid-context")
    if not settings.deepseek_api_key or not settings.embedding_api_key or not (settings.data_dir / "docqa.db").is_file():
        raise EvaluationStopped("缺少配置或隔离数据库，未开始调用")
    repository = Repository(settings.data_dir / "docqa.db")
    repository.initialize()
    config = {name: getattr(settings, name) for name in (
        "deepseek_base_url", "deepseek_model", "deepseek_thinking", "deepseek_reasoning_effort",
        "deepseek_max_tokens", "embedding_base_url", "embedding_model", "embedding_batch_size",
        "rag_retrieval_k", "rag_context_k", "question_context_k", "rag_context_max_chars",
        "rag_input_max_chars", "rag_min_score", "question_context_max_chars", "question_input_max_chars",
        "question_candidate_k")}
    if config != approval["unchanged_provider_and_budget_config"]:
        raise EvaluationStopped("模型、网关或预算配置已变化")
    index = DocumentIndex(repository, APIEmbedding(settings))
    sources = {}
    for case in cases:
        chunks = repository.chunks(case["document_id"], case["parse_version_id"])
        info = index.index_info(case["document_id"])
        if (digest([c.model_dump() for c in chunks]) != digest(case["chunks"])
                or info["status"] != "indexed" or info["parse_version_id"] != case["parse_version_id"]):
            raise EvaluationStopped("原文或可复用索引已变化；不自动建索引")
        sources[case["document_id"]] = {"chunks_sha256": digest(case["chunks"]), "index_id": info["index_id"]}
    for original in approval["originals"]:
        if sha256(Path(original["path"]).read_bytes()).hexdigest() != original["sha256"]:
            raise EvaluationStopped("原件哈希已变化")
    frozen = {"approval_sha256": digest(approval), "plan_sha256": digest(plan),
              "cases_sha256": digest(cases), "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
              "code_fingerprint": code_fingerprint(), "config": config, "sources": sources}
    stage, fingerprint = freeze_run(directory, frozen, repair_revision)
    budget = EvaluationBudget(directory / "request-ledger.db", limit=40, fingerprint=fingerprint)
    state_path = stage / "state.json"
    state = read(state_path) if state_path.exists() else {"queries": {}, "answers": {}}
    if state.get("stopped"):
        raise EvaluationStopped("已有停止记录；不能自动重试")
    outputs = stage / "outputs"
    outputs.mkdir(exist_ok=True)
    queries = directory / "queries"
    queries.mkdir(exist_ok=True)

    def persist():
        save(state_path, state)
        save(directory / "request-ledger.json", budget.report())

    import httpx
    from openai import OpenAI
    import app.deepseek
    import app.embedding

    context = ContextVar("live_evaluation_http_context", default=None)

    def factory(**kwargs):
        current = context.get()
        if current is None:
            raise EvaluationStopped("外调没有明确归属")
        client = httpx.Client(transport=BudgetTransport(budget, role=current[0], context=current[1]),
                              timeout=kwargs["timeout"], trust_env=False, follow_redirects=False)
        return OpenAI(**kwargs, http_client=client)

    previous = app.embedding.OpenAI, app.deepseek.OpenAI
    app.embedding.OpenAI = app.deepseek.OpenAI = factory

    class RecordedModel:
        def __init__(self, label):
            self.label, self.model = label, DeepSeekModel(settings)
        @property
        def last_call_metadata(self):
            return self.model.last_call_metadata
        def generate(self, system, user):
            path = outputs / (self.label + ".request.json")
            if path.exists():
                raise EvaluationStopped("已有发送意图，不自动重发")
            with path.open("x", encoding="utf-8") as stream:
                json.dump({"system": system, "user": json.loads(user)}, stream, ensure_ascii=False, indent=2)
            token = context.set(("generation", self.label))
            try:
                raw = self.model.generate(system, user)
            finally:
                context.reset(token)
            (outputs / (self.label + ".final.txt")).write_text(raw, encoding="utf-8")
            return raw

    try:
        selected_cases=cases
        if repair_revision=="fact-contract-v6":
            targets=read(directory/"repair-fact-contract-v6.json")["targets"]
            selected_cases=[next(c for c in cases if c["case_id"]==target) for target in targets]
        for case in selected_cases:
            if (code_fingerprint() != frozen["code_fingerprint"]
                    or sha256(Path(__file__).read_bytes()).hexdigest() != frozen["runner_sha256"]):
                raise EvaluationStopped("执行期间代码变化，停止外调")
            key, doc_id = case["case_id"].replace(":", "-"), case["document_id"]
            rag = RagService(settings, repository, index, RecordedModel(key))
            snapshot = rag.capture_snapshot(doc_id, case["question"])
            query_path = queries / (key + ".json")
            if query_path.exists():
                saved = read(query_path)
                saved_scope = dict(saved["snapshot"])
                current_scope = dict(snapshot)
                if repair_revision:
                    # embedding不依赖生成提示词；只允许该签名变化，所有来源/索引签名仍严格相同。
                    for name in ("configuration_signature","answer_contract_version","task_prompt_version"):
                        saved_scope.pop(name,None)
                        current_scope.pop(name,None)
                if saved_scope != current_scope or digest(saved["result"]) != saved["result_sha256"]:
                    raise EvaluationStopped("共享查询检查点变化")
                raw = saved["result"]
            else:
                if repair_revision=="fact-contract-v6":
                    raise EvaluationStopped("本次仅允许复用已验证查询，缺失时不新发embedding")
                intent = queries / (key + ".intent.json")
                with intent.open("x", encoding="utf-8") as stream:
                    json.dump({"case_id": case["case_id"], "snapshot": snapshot}, stream)
                token = context.set(("query", case["case_id"]))
                try:
                    raw = index.search_snapshot(doc_id, case["question"], 20, snapshot=snapshot, include_scores=True)
                finally:
                    context.reset(token)
                save(query_path, {"snapshot": snapshot, "result": raw, "result_sha256": digest(raw)})
            index.validate_checkpoint(doc_id, snapshot, raw)
            state["queries"][key] = {"path": str(query_path), "timings_ms": raw["timings_ms"]}
            persist()
            for branch in (["current"] if repair_revision=="fact-contract-v6" else plan["branches"]):
                label = key + "-" + branch
                rag = RagService(settings, repository, index, RecordedModel(label))
                base_prompt = build_task_system_prompt(include_table_structure=False)
                cap = rag._context_cap(base_prompt, case["question"], snapshot["filename"])
                retrieved = branch_retrieval(case, branch, raw,
                    repository.chunks(doc_id, case["parse_version_id"]), cap)
                index.validate_checkpoint(doc_id, snapshot, retrieved)
                built = build_evidence(retrieved, settings, context_cap=cap,
                                       expected_document_id=doc_id, payload_transform=task_evidence_payload,
                                       table_blocks=repository.blocks(case["parse_version_id"]),
                                       include_qualifier_cues=True,
                                       preserve_document_order=True,
                                       max_items=settings.question_context_k,context_max_chars=settings.question_context_max_chars,
                                       extractive_question=case['question'],
                                       table_prompt_overhead=len(build_task_system_prompt()) - len(base_prompt))
                if built.fatal_reason or not built.pack.items:
                    raise EvaluationStopped("没有可生成的合格证据")
                if branch == "oracle" and {i.chunk_id for i in built.pack.items} != {h["chunk_id"] for h in retrieved["results"]}:
                    raise EvaluationStopped("完整证据超出相同预算，停止比较")
                service = QuestionJobService(settings, repository, rag)
                if repair_revision == "source-title-spacing":
                    parent = directory / "revisions" / "prompt-v5"
                    prior = read(parent / "state.json")["answers"].get(label)
                    if prior and prior["status"] == "succeeded":
                        old_record = read(parent / "outputs" / (label + ".result.json"))
                        revalidate_saved_answer(old_record, read(parent / "outputs" / (label + ".request.json")),
                            (parent / "outputs" / (label + ".final.txt")).read_text(encoding="utf-8"),
                            built.pack, case=case, snapshot=snapshot,
                            prior_snapshot=json.loads(service.store.row(prior["job_id"])["snapshot_json"]))
                        old_record = {**old_record, "reuse": {"from": str(parent / "outputs"),
                            "strict_protocol_revalidated": True, "provider_calls": 0}}
                        save(outputs / (label + ".result.json"), old_record)
                        markdown, _, _ = service.export(old_record["result"]["id"], "markdown")
                        (outputs / (label + ".md")).write_text(markdown, encoding="utf-8")
                        state["answers"][label] = {**prior, "reused": True}
                        persist()
                        continue
                response, _ = service.submit(doc_id, {"question": case["question"], "idempotency_key": directory.name + "-" + (repair_revision + "-" if repair_revision else "") + label})
                job = response["job"]
                if job["status"] in {"failed", "needs_attention", "cancelled", "running"}:
                    raise EvaluationStopped("已有失败或在途任务，不自动重试")
                state["answers"][label] = {"job_id": job["id"], "status": job["status"]}
                persist()
                if job["status"] != "succeeded":
                    worker = QuestionWorker(settings, repository, rag=rag)
                    claimed = worker.store.claim(worker.worker_id, settings.worker_lease_seconds, job_id=job["id"])
                    if claimed is None or not worker.store.save_checkpoint(job["id"], claimed["lease_token"],
                            role="retrieval", payload=retrieved, sequence_no=None):
                        raise EvaluationStopped("无法固定分支检索检查点")
                    worker.process_job(claimed)
                job = service.get_job(job["id"])
                record = {"job": job, "case_sha256": digest(case), "branch": branch,
                          "retrieval": evidence_metrics(case, [h["chunk_id"] for h in retrieved["results"]],
                              [i.chunk_id for i in built.pack.items]),
                          "selected_chunk_ids": [i.chunk_id for i in built.pack.items],
                          "context_chars": built.pack.context_chars, "shared_query_timings_ms": raw["timings_ms"],
                          "planning": retrieved.get("planning", {}), "semantic_acceptance": "pending_manual_review"}
                if job["result_id"]:
                    record["result"] = service.get_result(job["result_id"])
                    markdown, _, _ = service.export(job["result_id"], "markdown")
                    (outputs / (label + ".md")).write_text(markdown, encoding="utf-8")
                save(outputs / (label + ".result.json"), record)
                state["answers"][label] = {"job_id": job["id"], "status": job["status"], "error_code": job["error_code"]}
                persist()
                print(json.dumps({"case": label, "status": job["status"], "code": job["error_code"], "used": budget.report()["used"]}), flush=True)
                if job["status"] != "succeeded":
                    raise EvaluationStopped("首次工程或上游失败，停止后续外调")
        state["execution_complete"] = True
        state["semantic_acceptance"] = "pending_manual_review"
        persist()
        return {"mode": "live", "used": budget.report()["used"], "answers": len(state["answers"]),
                "remaining": budget.report()["remaining"], "semantic_acceptance": "pending_manual_review"}
    except BaseException as exc:
        state["stopped"] = {"error_type": type(exc).__name__, "reason": str(exc) if isinstance(exc, EvaluationStopped) else "调用或本地步骤失败，保留检查点"}
        persist()
        raise
    finally:
        app.embedding.OpenAI, app.deepseek.OpenAI = previous


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--allow-online", action="store_true")
    parser.add_argument("--repair-revision", choices=["prompt-v5", "source-title-spacing", "fact-contract-v6"])
    args = parser.parse_args(argv)
    try:
        result = run(args.run_dir, allow_online=args.allow_online, repair_revision=args.repair_revision)
    except Exception as exc:
        print(json.dumps({"status": "stopped", "error_type": type(exc).__name__}), flush=True)
        return 2
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
