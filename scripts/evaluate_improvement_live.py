"""冻结开发样本的真实检索对照；默认只规划，显式在线且累计HTTP预算不超过40。"""
from __future__ import annotations

import argparse
from contextvars import ContextVar
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.deepseek import DeepSeekModel
from app.embedding import APIEmbedding
from app.evaluation_budget import BudgetTransport, EvaluationBudget, EvaluationStopped
from app.question_jobs import QuestionJobService
from app.question_worker import QuestionWorker
from app.rag import RagService
from app.repository import Repository
from app.runtime import code_fingerprint
from app.vector_index import DocumentIndex
from app.rag_prompts import TASK_PROMPT_VERSION


def read_plan(directory: Path):
    directory = directory.resolve()
    formal = Settings.from_env().data_dir.resolve()
    allowed = (ROOT / "data").resolve()
    if (not directory.is_relative_to(allowed) or directory == allowed or directory == formal
            or formal.is_relative_to(directory) or not (directory / "approval.json").is_file()):
        raise EvaluationStopped("需要data下独立验收目录及明确批准记录")
    manifest_path = directory / "frozen-cases.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    approval = json.loads((directory / "approval.json").read_text(encoding="utf-8"))
    if (manifest.get("schema") != "docqa-live-comparison-v1" or manifest.get("request_limit") != 40
            or approval.get("total_request_limit") != 40 or len(manifest.get("documents", [])) != 3
            or manifest.get("strategies") != ["vector", "hybrid-context"]):
        raise EvaluationStopped("范围或批准记录不符合本阶段3文档／6题／40次上限")
    identifiers = set()
    for document in manifest["documents"]:
        if document.get("status") != "succeeded" or len(document.get("questions", [])) != 2:
            raise EvaluationStopped("解析或题目清单不完整")
        if sha256(Path(document["original_path"]).read_bytes()).hexdigest() != document["sha256"]:
            raise EvaluationStopped("冻结原件哈希已变化")
        for question in document["questions"]:
            if not question.get("question") or not question.get("required_points") or question["id"] in identifiers:
                raise EvaluationStopped("题目重复或预标注缺失")
            identifiers.add(question["id"])
    return directory, manifest


def run(directory: Path, *, allow_online: bool = False, revision: str | None = None):
    directory, manifest = read_plan(directory)
    settings = replace(Settings.from_env(), data_dir=directory / "run-data", allow_database_upgrade=False,
                       rag_retrieval_k=20, rag_context_k=20, question_context_k=20)
    repository = Repository(settings.data_dir / "docqa.db")
    repository.initialize()
    local_index = DocumentIndex(repository, APIEmbedding(settings))
    planned = 24 + sum(0 if local_index.index_info(d["document_id"])["status"] == "indexed" else
                       (len(repository.chunks(d["document_id"], d["parse_version_id"])) + settings.embedding_batch_size - 1)
                       // settings.embedding_batch_size for d in manifest["documents"])
    if revision == "citation-choices-v3":
        # 12次生成；论文4个真实检索检查点复用，另4个问题的查询向量各只请求一次。
        planned -= 8
    if not allow_online:
        return {"mode": "plan_only", "paid_calls_this_run": 0, "planned_requests": planned, "limit": 40}
    if planned > 40 or not settings.deepseek_api_key or not settings.embedding_api_key:
        raise EvaluationStopped("计划超过40次或模型缺配置，未开始外调")
    config = {name: getattr(settings, name) for name in (
        "deepseek_base_url", "deepseek_model", "deepseek_thinking", "deepseek_reasoning_effort",
        "deepseek_max_tokens", "embedding_base_url", "embedding_model", "embedding_batch_size",
        "rag_retrieval_k", "rag_context_k", "question_context_k", "rag_context_max_chars", "rag_input_max_chars", "rag_min_score")}
    frozen = {"manifest_sha256": sha256((directory / "frozen-cases.json").read_bytes()).hexdigest(),
              "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
              "code_fingerprint": code_fingerprint(), "config": config}
    fingerprint = sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
    frozen_path = directory / "runtime-freeze.json"
    if revision is not None:
        expected_prompts = {"citation-spans-v2": "rag-task-v2", "citation-choices-v3": "rag-task-v3"}
        if expected_prompts.get(revision) != TASK_PROMPT_VERSION or not frozen_path.exists():
            raise EvaluationStopped("只有已记录的连续范围协议修复可进入本轮对照修订")
        original = json.loads(frozen_path.read_text(encoding="utf-8"))
        if original["manifest_sha256"] != frozen["manifest_sha256"] or original["config"] != frozen["config"]:
            raise EvaluationStopped("修复轮不能改变问题、预期、原件或模型配置")
        # 只增加修订记录，保留同一批准行、累计计数和首次失败；不能新建账本借额度。
        with sqlite3.connect(directory / "request-ledger.db") as db:
            record = db.execute("SELECT max_requests,fingerprint FROM approval WHERE id=1").fetchone()
        if record is None or record[0] != 40:
            raise EvaluationStopped("缺少本轮已批准的累计账本")
        fingerprint = record[1]
        frozen_path = directory / ("runtime-freeze-" + revision + ".json")
    budget = EvaluationBudget(directory / "request-ledger.db", limit=40, fingerprint=fingerprint)
    if frozen_path.exists() and json.loads(frozen_path.read_text(encoding="utf-8")) != frozen:
        raise EvaluationStopped("冻结配置已变化，不能继续本轮调用")
    if not frozen_path.exists():
        frozen_path.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")

    # 实际SDK使用同一传输包装，每一个embedding批次和生成HTTP均在发送前预扣。
    import httpx
    from openai import OpenAI
    import app.deepseek
    import app.embedding

    call_context = ContextVar("evaluation_call_context", default=None)

    def factory(**kwargs):
        current = call_context.get()
        if current is None:
            raise EvaluationStopped("外调没有固定步骤归属")
        role, context = current
        client = httpx.Client(transport=BudgetTransport(budget, role=role, context=context),
                              timeout=kwargs["timeout"], trust_env=False, follow_redirects=False)
        return OpenAI(**kwargs, http_client=client)

    previous_embedding, previous_generation = app.embedding.OpenAI, app.deepseek.OpenAI
    app.embedding.OpenAI = app.deepseek.OpenAI = factory
    outputs = directory / "outputs"
    outputs.mkdir(exist_ok=True)
    state_path = directory / ("comparison-state-" + revision + ".json" if revision else "comparison-state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"indexes": {}, "answers": {}}

    def save():
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        (directory / "request-ledger.json").write_text(json.dumps(budget.report(), ensure_ascii=False, indent=2), encoding="utf-8")

    class CachedQueryEmbedding:
        """仅本次对照共享相同问题的真实向量；不缓存索引请求或改变模型输入。"""
        def __init__(self, case_settings):
            self.real = APIEmbedding(case_settings)
            self.settings = case_settings
            self.metadata = None
        @property
        def signature(self): return self.real.signature
        @property
        def last_call_metadata(self): return self.metadata
        def embed(self, texts):
            from app.embedding import normalize_vector
            key = sha256(json.dumps({"signature": self.signature, "texts": texts}, sort_keys=True).encode()).hexdigest()
            cache = directory / "query-cache" / (key + ".json")
            if cache.exists():
                cached = json.loads(cache.read_text(encoding="utf-8"))
                vectors = cached["vectors"]
                digest = sha256(json.dumps(vectors).encode()).hexdigest()
                if digest != cached["sha256"] or len(vectors) != len(texts):
                    raise EvaluationStopped("保存的查询向量校验失败")
                for vector in vectors: normalize_vector(vector)
                self.metadata = {"usage_status": "local_query_cache", "usage": None}
                return vectors
            vectors = self.real.embed(texts)
            self.metadata = self.real.last_call_metadata
            cache.parent.mkdir(exist_ok=True)
            with cache.open("x", encoding="utf-8") as stream:
                json.dump({"vectors": vectors, "sha256": sha256(json.dumps(vectors).encode()).hexdigest()}, stream)
            return vectors

    class RecordedModel:
        def __init__(self, model, label): self.model, self.label = model, label
        @property
        def last_call_metadata(self): return self.model.last_call_metadata
        def generate(self, system, user):
            path = outputs / (self.label + ".request.json")
            if path.exists():
                raise EvaluationStopped("该题已有生成请求记录，不自动重发")
            path.write_text(json.dumps({"system": system, "user": json.loads(user)}, ensure_ascii=False, indent=2), encoding="utf-8")
            token = call_context.set(("generation", self.label))
            try: raw = self.model.generate(system, user)
            finally: call_context.reset(token)
            # 只记录适配器返回的最终正文；供应商思考不进入证据文件。
            (outputs / (self.label + ".final.txt")).write_text(raw, encoding="utf-8")
            return raw

    try:
        for document in manifest["documents"]:
            doc_id = document["document_id"]
            current = repository.get(doc_id)
            if current is None or current.active_parse_version_id != document["parse_version_id"]:
                raise EvaluationStopped("固定解析版本已变化")
            if len(repository.chunks(doc_id, document["parse_version_id"])) != document["chunk_count"]:
                raise EvaluationStopped("固定分块数量已变化")
            token = call_context.set(("index", document["key"]))
            try:
                index = DocumentIndex(repository, APIEmbedding(settings))
                info = index.build(doc_id)
            finally: call_context.reset(token)
            state["indexes"][document["key"]] = info
            save()
            print(json.dumps({"document": document["key"], "stage": "indexed", "used": budget.report()["used"]}), flush=True)

            for question in document["questions"]:
                for strategy in manifest["strategies"]:
                    label = ((revision + "-") if revision else "") + question["id"].replace(":", "-") + "-" + strategy
                    case_settings = replace(settings, question_retrieval_strategy=strategy)
                    query_embedding = CachedQueryEmbedding(case_settings) if revision == "citation-choices-v3" else APIEmbedding(case_settings)
                    rag = RagService(case_settings, repository, DocumentIndex(repository, query_embedding),
                                     RecordedModel(DeepSeekModel(case_settings), label))
                    service = QuestionJobService(case_settings, repository, rag)
                    response, _ = service.submit(doc_id, {"question": question["question"], "idempotency_key": label})
                    job = response["job"]
                    if job["status"] in {"failed", "needs_attention", "cancelled", "running"}:
                        raise EvaluationStopped("已有失败或未确认任务；本入口不自动重试")
                    if job["status"] != "succeeded":
                        state["answers"][label] = {"job_id": job["id"], "status": job["status"]}
                        save()
                        token = call_context.set(("query", label))
                        try:
                            worker = QuestionWorker(case_settings, repository, rag=rag)
                            if revision == "citation-choices-v3" and document["key"] == "paper":
                                # 原问题／策略／索引相同，只修复引述协议；复用真实检索检查点，仍重新核对原文及来源。
                                old_label = "citation-spans-v2-" + question["id"].replace(":", "-") + "-" + strategy
                                old_state = json.loads((directory / "comparison-state-citation-spans-v2.json").read_text(encoding="utf-8"))
                                prior_id = old_state["answers"][old_label]["id"]
                                retrieval = service.store.checkpoint(prior_id, "retrieval")
                                snapshot = json.loads(service.store.row(job["id"])["snapshot_json"])
                                rag.index.validate_checkpoint(doc_id, snapshot, retrieval)
                                claimed = service.store.claim(worker.worker_id, case_settings.worker_lease_seconds, job_id=job["id"])
                                if claimed is None or not service.store.save_checkpoint(job["id"], claimed["lease_token"], role="retrieval", payload=retrieval, sequence_no=None):
                                    raise EvaluationStopped("无法安全恢复已固定的检索证据")
                                worker.process_job(claimed)
                            else:
                                worker.run_job(job["id"])
                        finally: call_context.reset(token)
                    job = service.get_job(job["id"])
                    state["answers"][label] = job
                    if job["result_id"]:
                        result = service.get_result(job["result_id"])
                        (outputs / (label + ".result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                        markdown, _, _ = service.export(job["result_id"], "markdown")
                        (outputs / (label + ".md")).write_text(markdown, encoding="utf-8")
                    save()
                    print(json.dumps({"case": label, "status": job["status"], "error_code": job["error_code"],
                                      "used": budget.report()["used"]}), flush=True)
                    if job["status"] != "succeeded":
                        raise EvaluationStopped("首次工程失败已保存，停止后续外调")
        state["execution_complete"] = True
        state["semantic_acceptance"] = "pending_manual_review"
        save()
        return {"mode": "live", "completed_answers": len(state["answers"]), "used": budget.report()["used"],
                "remaining": budget.report()["remaining"], "semantic_acceptance": "pending_manual_review"}
    except Exception as exc:
        state["stopped"] = {"error_type": type(exc).__name__, "reason": str(exc) if isinstance(exc, EvaluationStopped) else "外调或本地步骤失败，原始证据与账本保留"}
        save()
        raise
    finally:
        app.embedding.OpenAI, app.deepseek.OpenAI = previous_embedding, previous_generation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--allow-online", action="store_true")
    parser.add_argument("--revision", choices=["citation-spans-v2", "citation-choices-v3"], help="只修复连续引述选择协议；同账本累计，不重置首次失败")
    args = parser.parse_args(argv)
    try:
        result = run(args.run_dir, allow_online=args.allow_online, revision=args.revision)
    except Exception as exc:
        print(json.dumps({"status": "stopped", "error_type": type(exc).__name__}), flush=True)
        return 2
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
