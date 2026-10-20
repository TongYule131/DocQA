"""新批准阶段的四题定向复验；默认离线，禁止新查询／建索引，逐HTTP累计。"""
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
from app.retrieval_planning import RETRIEVAL_STRATEGY_VERSION, plan_retrieval
from app.repository import Repository
from app.runtime import code_fingerprint
from app.vector_index import DocumentIndex

TARGETS = ["xlsx-revenue:Q2", "xlsx-revenue:Q4", "docx-learning:Q4", "paper:Q3"]
CONFIG_FIELDS = ("deepseek_base_url", "deepseek_model", "deepseek_thinking", "deepseek_reasoning_effort",
    "deepseek_max_tokens", "embedding_base_url", "embedding_model", "embedding_batch_size", "rag_retrieval_k",
    "rag_context_k", "question_context_k", "rag_context_max_chars", "rag_input_max_chars", "rag_min_score")
QUERY_FIELDS = ("document_id", "filename", "start_version_id", "parse_version_id", "index_id", "is_legacy",
    "source_signature", "source_signature_algo", "provider_signature", "model_signature", "evidence_signature")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_scope(directory, *, revision=None):
    directory = Path(directory).resolve()
    allowed, formal = (ROOT / "data").resolve(), (ROOT / "data").resolve()
    if not directory.is_relative_to(allowed) or directory == formal:
        raise EvaluationStopped("需要data下新的独立验收目录")
    approval, plan, cases = (read(directory / name) for name in ("approval.json", "plan.json", "cases.json"))
    if (approval.get("authorized") is not True or approval.get("stage") != "rag-targeted-v8-20261019"
            or approval.get("total_request_limit") != 40 or not approval.get("user_reply")
            or approval.get("plan_sha256") != digest(plan) or plan.get("cases_sha256") != digest(cases)
            or plan.get("schema") != "docqa-rag-targeted-live-v1" or plan.get("targets") != TARGETS
            or [c.get("case_id") for c in cases] != TARGETS or len({c["document_id"] for c in cases}) != 3
            or plan.get("planned_requests") != {"query": 0, "index": 0, "generation": 4, "total": 4}
            or plan.get("retrieval_version") != RETRIEVAL_STRATEGY_VERSION):
        raise EvaluationStopped("新阶段批准或冻结范围无效，不能复用历史额度")
    if revision is None and (plan.get("task_prompt_version") != TASK_PROMPT_VERSION
            or plan.get("full_prompt_sha256") != digest(build_task_system_prompt())
            or plan.get("base_prompt_sha256") != digest(build_task_system_prompt(include_table_structure=False))):
        raise EvaluationStopped("提示词与计划不一致")
    for case in cases:
        validate_case(case)
    return directory, approval, plan, cases


class CachedEmbedding(APIEmbedding):
    def embed(self, texts):
        raise EvaluationStopped("本阶段仅复用查询缓存，禁止新增embedding或索引请求")


def prepare(repository, settings, case, saved_query):
    """只读原件、索引及查询缓存，并经生产证据构建器冻结将要发送的消息。"""
    index = DocumentIndex(repository, CachedEmbedding(settings))
    rag = RagService(settings, repository, index, None)
    snapshot = rag.capture_snapshot(case["document_id"], case["question"])
    if (any(saved_query["snapshot"].get(k) != snapshot.get(k) for k in QUERY_FIELDS)
            or digest(saved_query["result"]) != saved_query.get("result_sha256")):
        raise EvaluationStopped("查询缓存的来源、定位、索引或模型已变化")
    chunks = repository.chunks(case["document_id"], case["parse_version_id"])
    if digest([c.model_dump() for c in chunks]) != digest(case["chunks"]):
        raise EvaluationStopped("冻结案例与解析原文不一致")
    raw = index.validate_checkpoint(case["document_id"], snapshot, saved_query["result"])
    base = build_task_system_prompt(include_table_structure=False)
    cap = rag._context_cap(base, case["question"], snapshot["filename"])
    retrieved = plan_retrieval(case["question"], raw, chunks, fulltext_budget=cap,
                               max_items=settings.question_context_k, payload_transform=task_evidence_payload)
    index.validate_checkpoint(case["document_id"], snapshot, retrieved)
    pack = build_evidence(retrieved, settings, expected_document_id=case["document_id"], context_cap=cap,
        payload_transform=task_evidence_payload, table_blocks=repository.blocks(case["parse_version_id"]),
        table_prompt_overhead=len(build_task_system_prompt()) - len(base), include_qualifier_cues=True,
        preserve_document_order=True).pack
    if not pack.items:
        raise EvaluationStopped("没有合格输入，不生成")
    system = build_task_system_prompt(include_table_structure=any(i.table_projection_included for i in pack.items))
    user = json.loads(build_task_user_prompt(question=case["question"], document_name=snapshot["filename"], evidence_payload=pack.payload()))
    return dict(snapshot=snapshot, retrieved=retrieved, request={"system": system, "user": user},
                selected_chunk_ids=[i.chunk_id for i in pack.items], context_chars=pack.context_chars,
                retrieval=evidence_metrics(case, [h["chunk_id"] for h in retrieved["results"]], [i.chunk_id for i in pack.items]))


def freeze_stage(directory, frozen, revision=None):
    """实质修复保留原累计账本；继续失败题及首次停止后尚未执行的题。"""
    original_path = directory / "runtime-freeze.json"
    if revision is None:
        if original_path.exists() and read(original_path) != frozen:
            raise EvaluationStopped("代码或冻结输入变化，需要记录实质修复，不自动重发")
        if not original_path.exists():
            save(original_path, frozen)
        return directory, digest(frozen), TARGETS
    if not revision or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in revision):
        raise EvaluationStopped("修订名称无效")
    original = read(original_path)
    repair = read(directory / ("repair-" + revision + ".json"))
    parent = directory if repair.get("parent_stage") == "." else directory / "revisions" / str(repair.get("parent_stage", ""))
    if not parent.resolve().is_relative_to(directory.resolve()):
        raise EvaluationStopped("修订不能越出当前阶段")
    review = read(parent / "semantic-review.json")
    parent_freeze = read(parent / "runtime-freeze.json")
    fixed_keys = ("approval_sha256", "plan_sha256", "cases_sha256", "config", "sources", "queries")
    if (any(frozen[k] != original[k] for k in fixed_keys)
            or repair.get("original_freeze_sha256") != digest(original)
            or repair.get("parent_review_sha256") != digest(review)
            or review.get("overall") != "not_fully_accepted" or not repair.get("reason")
            or repair.get("same_stage_limit") != 40 or repair.get("no_ledger_reset") is not True
            or repair.get("targets") != review.get("remaining_case_ids") or not repair.get("targets")
            or any(t not in TARGETS for t in repair["targets"])
            or frozen["code_fingerprint"] == parent_freeze["code_fingerprint"]
            or not (directory / "request-ledger.db").is_file()):
        raise EvaluationStopped("缺少同阶段实质修复及未完成依据，不能重置额度或重跑通过题")
    stage = directory / "revisions" / revision
    stage.mkdir(parents=True, exist_ok=True)
    frozen = {**frozen, "repair_sha256": digest(repair)}
    path = stage / "runtime-freeze.json"
    if path.exists() and read(path) != frozen:
        raise EvaluationStopped("已冻结的修订发生变化")
    if not path.exists():
        save(path, frozen)
    return stage, digest(original), repair["targets"]


def run(directory, *, allow_online=False, revision=None):
    directory, approval, plan, cases = load_scope(directory, revision=revision)
    if not allow_online:
        return dict(mode="plan_only", provider_calls_this_run=0, planned=4, limit=40)
    env = Settings.from_env()
    if directory == env.data_dir.resolve() or env.data_dir.resolve().is_relative_to(directory):
        raise EvaluationStopped("验收目录不能覆盖正式数据目录")
    settings = replace(env, data_dir=directory / "run-data", allow_database_upgrade=False,
        rag_retrieval_k=20, rag_context_k=20, question_context_k=20, question_retrieval_strategy="hybrid-context")
    config = {name: getattr(settings, name) for name in CONFIG_FIELDS}
    if config != approval["unchanged_provider_and_budget_config"]:
        raise EvaluationStopped("模型、网关或预算配置变化")
    for original in approval["originals"]:
        if file_hash(original["path"]) != original["sha256"]:
            raise EvaluationStopped("原件发生变化")
    if not settings.deepseek_api_key or not settings.embedding_api_key or not (settings.data_dir / "docqa.db").is_file():
        raise EvaluationStopped("缺少配置或隔离数据库")
    repository = Repository(settings.data_dir / "docqa.db")
    repository.initialize()
    prepared, queries, sources = {}, {}, {}
    for case in cases:
        key = case["case_id"].replace(":", "-")
        path = directory / "queries" / (key + ".json")
        if file_hash(path) != plan["query_files_sha256"][case["case_id"]]:
            raise EvaluationStopped("冻结的查询文件已变化")
        prepared[case["case_id"]] = prepare(repository, settings, case, read(path))
        queries[case["case_id"]] = file_hash(path)
        snapshot = prepared[case["case_id"]]["snapshot"]
        sources[case["document_id"]] = {k: snapshot[k] for k in (*QUERY_FIELDS, "table_structure_signature")}
    if revision is None and digest(prepared) != plan.get("input_preview_sha256"):
        raise EvaluationStopped("实际输入与批准前冻结预览不同")
    frozen = dict(approval_sha256=digest(approval), plan_sha256=digest(plan), cases_sha256=digest(cases),
        config=config, sources=sources, queries=queries, code_fingerprint=code_fingerprint(),
        runner_sha256=file_hash(__file__), inputs_sha256=digest(prepared))
    stage, fingerprint, targets = freeze_stage(directory, frozen, revision)
    budget = EvaluationBudget(directory / "request-ledger.db", limit=40, fingerprint=fingerprint)
    state_path = stage / "state.json"
    state = read(state_path) if state_path.exists() else {"answers": {}, "execution_complete": False}
    if state.get("stopped"):
        raise EvaluationStopped("已有停止记录，不自动重试")
    outputs = stage / "outputs"
    outputs.mkdir(exist_ok=True)

    def persist():
        save(state_path, state)
        save(directory / "request-ledger.json", budget.report())

    import httpx
    from openai import OpenAI
    import app.deepseek
    context = ContextVar("targeted_evaluation_http", default=None)

    def factory(**kwargs):
        label = context.get()
        if label not in targets:
            raise EvaluationStopped("不在本次冻结的生成范围内")
        client = httpx.Client(transport=BudgetTransport(budget, role="generation", context=label),
                              timeout=kwargs["timeout"], trust_env=False, follow_redirects=False)
        return OpenAI(**kwargs, http_client=client)

    class RecordedModel:
        def __init__(self, case_id):
            self.case_id, self.model = case_id, DeepSeekModel(settings)
        @property
        def last_call_metadata(self):
            return self.model.last_call_metadata
        def generate(self, system, user):
            request = {"system": system, "user": json.loads(user)}
            if request != prepared[self.case_id]["request"]:
                raise EvaluationStopped("实际生成输入与冻结预览不同")
            path = outputs / (self.case_id.replace(":", "-") + ".request.json")
            with path.open("x", encoding="utf-8") as stream:
                json.dump(request, stream, ensure_ascii=False, indent=2)
            token = context.set(self.case_id)
            try:
                raw = self.model.generate(system, user)
            finally:
                context.reset(token)
            path.with_suffix(".final.txt").write_text(raw, encoding="utf-8")
            return raw

    previous = app.deepseek.OpenAI
    app.deepseek.OpenAI = factory
    persist()
    try:
        for case in cases:
            case_id = case["case_id"]
            if case_id not in targets or case_id in state["answers"]:
                continue
            if (code_fingerprint() != frozen["code_fingerprint"] or file_hash(__file__) != frozen["runner_sha256"]
                    or digest(read(directory / "approval.json")) != frozen["approval_sha256"]):
                raise EvaluationStopped("执行期间代码或批准变化")
            ready = prepared[case_id]
            rag = RagService(settings, repository, DocumentIndex(repository, CachedEmbedding(settings)), RecordedModel(case_id))
            service = QuestionJobService(settings, repository, rag)
            reply, _ = service.submit(case["document_id"], {"question": case["question"],
                "idempotency_key": directory.name + "-" + (revision or "base") + "-" + case_id})
            job = reply["job"]
            if job["status"] != "queued" or json.loads(service.store.row(job["id"])["snapshot_json"]) != ready["snapshot"]:
                raise EvaluationStopped("已有在途／终态任务或输入变化，不自动重发")
            worker = QuestionWorker(settings, repository, rag=rag)
            claimed = worker.store.claim(worker.worker_id, settings.worker_lease_seconds, job_id=job["id"])
            if not claimed or not worker.store.save_checkpoint(job["id"], claimed["lease_token"], role="retrieval",
                                                             payload=ready["retrieved"], sequence_no=None):
                raise EvaluationStopped("无法固定检索检查点")
            worker.process_job(claimed)
            job = service.get_job(job["id"])
            record = dict(job=job, case_sha256=digest(case), selected_chunk_ids=ready["selected_chunk_ids"],
                retrieval=ready["retrieval"], context_chars=ready["context_chars"], semantic_acceptance="pending_manual_review")
            if job["result_id"]:
                record["result"] = service.get_result(job["result_id"])
                (outputs / (case_id.replace(":", "-") + ".md")).write_text(service.export(job["result_id"], "markdown")[0], encoding="utf-8")
            save(outputs / (case_id.replace(":", "-") + ".result.json"), record)
            state["answers"][case_id] = {"job_id": job["id"], "status": job["status"], "error_code": job["error_code"]}
            persist()
            print(json.dumps(dict(case_id=case_id, status=job["status"], code=job["error_code"], used=budget.report()["used"])), flush=True)
            if job["status"] != "succeeded":
                raise EvaluationStopped("首次工程或上游失败，停止后续调用")
        state["execution_complete"] = True
        state["semantic_acceptance"] = "pending_manual_review"
        persist()
        return dict(mode="live", used=budget.report()["used"], remaining=budget.report()["remaining"], semantic_acceptance="pending_manual_review")
    except BaseException as exc:
        state["stopped"] = {"type": type(exc).__name__, "reason": str(exc) if isinstance(exc, EvaluationStopped) else "调用或本地处理失败"}
        persist()
        raise
    finally:
        app.deepseek.OpenAI = previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--allow-online", action="store_true")
    parser.add_argument("--revision")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.run_dir, allow_online=args.allow_online, revision=args.revision)), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps(dict(status="stopped", error_type=type(exc).__name__)), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
