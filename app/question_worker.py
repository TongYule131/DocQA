"""可恢复问答执行器；由已有 analysis-worker 循环调度，不增加常驻服务角色。

RagService 仍负责原有提示词、证据和引用校验。本模块只包围两个外调边界，
在回调前预扣，保存查询或已校验回答后才结算，明确区分安全恢复与不确定调用。
"""
from __future__ import annotations

import json
import logging
import os
import time
from threading import Event, Thread
from uuid import uuid4

from app.deepseek import ModelError
from app.embedding import EmbeddingError
from app.question_jobs import QuestionJobStore
from app.rag_context import RagError
from app.repository import Repository
from app.runtime import RuntimeGuard
from app.schemas import RagAnswer

logger = logging.getLogger("docqa.question")


class _Stopped(Exception):
    def __init__(self, code: str, message: str, status: str = "failed"):
        self.code, self.message, self.status = code, message, status


class QuestionWorker:
    def __init__(self, settings, repository: Repository | None = None, *, rag=None,
                 worker_id: str | None = None, runtime=None):
        self.settings = settings
        self.repository = repository or Repository(settings.data_dir / "docqa.db")
        self.store = QuestionJobStore(self.repository)
        self.runtime = runtime or RuntimeGuard(settings.data_dir, "analysis-worker")
        self.worker_id = worker_id or f"question-{os.getpid()}-{uuid4().hex[:8]}"
        self.stop_requested = False
        if rag is None:
            from app.deepseek import DeepSeekModel
            from app.embedding import APIEmbedding
            from app.rag import RagService
            from app.vector_index import DocumentIndex

            rag = RagService(settings, self.repository, DocumentIndex(self.repository, APIEmbedding(settings)),
                             DeepSeekModel(settings))
        self.rag = rag

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def run_once(self, *, job_id: str | None = None) -> bool:
        if self.stop_requested:
            return False
        self.runtime.check()
        job = self.store.claim(self.worker_id, self.settings.worker_lease_seconds, job_id=job_id)
        if job is None:
            return False
        self.process_job(job)
        return True

    def run_job(self, job_id: str) -> bool:
        return self.run_once(job_id=job_id)

    def process_job(self, job: dict) -> None:
        """心跳覆盖查询和生成；过期旧执行者无权发下一请求或保存结果。"""
        done = Event()

        def heartbeat():
            while not done.wait(max(0.2, self.settings.worker_lease_seconds / 3)):
                try:
                    if not self.store.renew(job["id"], job["lease_token"], self.settings.worker_lease_seconds):
                        return
                except Exception:
                    logger.error("问答任务 %s request_id=%s 续租失败", job["id"], job["request_id"])
                    return

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self._process_job(job)
        finally:
            done.set()
            thread.join(timeout=2)

    def _process_job(self, job: dict) -> None:
        job_id, token = job["id"], job["lease_token"]
        current_sequence: int | None = None
        call_started: float | None = None
        current_role: str | None = None

        def provider_usage():
            if current_role == "generation":
                return getattr(getattr(self.rag, "model", None), "last_call_metadata", None)
            if current_role == "embedding":
                return getattr(getattr(getattr(self.rag, "index", None), "embedding", None), "last_call_metadata", None)
            return None

        def check_active():
            self.runtime.check()
            if not self.store.lease_valid(job_id, token):
                raise _Stopped("lease_lost", "执行租约已失效，本次执行不再继续")
            if self.store.cancelled(job_id):
                raise _Stopped("cancelled", "已取消后续处理；在途请求可能继续计费", "cancelled")
            if self.stop_requested:
                raise _Stopped("worker_stopped", "执行者已停止，已保存检查点保留，可明确重试")

        def progress(stage: str, detail: str):
            check_active()
            if not self.store.stage(job_id, token, stage, detail):
                raise _Stopped("lease_lost", "执行租约已失效，本次执行不再继续")

        def observer(stage: str, details: dict):
            nonlocal current_sequence, call_started, current_role
            if stage in {'stream_started','stream_block','stream_check'}:
                check_active()
                if stage!='stream_check':
                    from app.answer_stream_store import AnswerStreamStore
                    AnswerStreamStore(self.repository).append('question',job,'reset' if stage=='stream_started' else 'block',details)
            elif stage in {"query_started", "generation_started"}:
                role = "embedding" if stage == "query_started" else "generation"
                current_role = role
                progress("retrieving" if role == "embedding" else "generating",
                         "正在查询并固定证据" if role == "embedding" else "正在生成回答")
                current_sequence = self.store.reserve(job_id, token, role)
                if current_sequence is None:
                    check_active()
                    raise _Stopped("budget_exhausted", "问答请求预算已用尽，未发起新的请求", "needs_attention")
                call_started = time.perf_counter()
                if isinstance(details.get("diagnostics"), dict):
                    if not self.store.save_checkpoint(job_id, token, role="evidence", payload=details["diagnostics"], sequence_no=None):
                        raise _Stopped("lease_lost", "执行租约已失效，本次执行不再继续")
            elif stage == "query_completed":
                # 检索来源固定后原子写检查点；重启恢复用同一版本、同一组候选证据。
                payload = details.get("search_result")
                if current_sequence is None or not isinstance(payload, dict):
                    raise _Stopped("invalid_retrieval_checkpoint", "检索检查点无效，已停止生成")
                if not self.store.save_checkpoint(job_id, token, role="retrieval", payload=payload,
                        sequence_no=current_sequence, elapsed_ms=_elapsed(call_started), provider_usage=details.get("provider_usage")):
                    raise _Stopped("lease_lost", "执行租约已失效，检索结果未保存")
                current_sequence, call_started = None, None
                current_role = None
                progress("evidence_ready", "证据已固定，正在核对输入范围")
            elif stage == "validation_started":
                progress("validating", "正在校验回答结构与原文引用")
            elif stage == "evidence_fixed" and isinstance(details.get("diagnostics"), dict):
                check_active()
                if not self.store.save_checkpoint(job_id, token, role="evidence", payload=details["diagnostics"], sequence_no=None):
                    raise _Stopped("lease_lost", "执行租约已失效，本次执行不再继续")

        try:
            check_active()
            # 未确认调用不会自动重发；只有显式 retry 才将其标记为已知风险。
            with self.repository.connect() as db:
                unresolved = db.execute("SELECT 1 FROM question_calls WHERE job_id=? AND"
                    " (status='intent' OR (status='uncertain' AND COALESCE(error_code,'')!='retry_acknowledged_uncertain'))",
                    (job_id,)).fetchone()
            if unresolved:
                raise _Stopped("call_uncertain", "存在未确认的调用，系统不会自动重发；请核实后明确重试", "needs_attention")
            snapshot = json.loads(job["snapshot_json"])
            resolution=snapshot.get('intent_resolution')
            if resolution:
                from app.intent_management import IntentManagement
                routing_started=time.perf_counter()
                current=IntentManagement(self.repository).evaluate(job['question'],snapshot.get('intent_base_id'),scope='document')
                if current!=resolution:raise _Stopped('intent_changed','意图配置已变化，请重新提交本轮')
                if resolution['route']['kind']=='SYSTEM':
                    from app.intent_replies import interaction_answer
                    answer=interaction_answer(job,snapshot,_elapsed(routing_started))
                    if not self.store.publish_interaction(job_id,token,answer):
                        check_active();raise _Stopped('interaction_publish_rejected','系统交互未保存，请刷新核对')
                    return
            saved_answer = self.store.checkpoint(job_id, "answer")
            if saved_answer is not None:
                answer = RagAnswer.model_validate(saved_answer).model_dump()
            else:
                retrieval = self.store.checkpoint(job_id, "retrieval")
                if retrieval is not None:
                    progress("evidence_ready", "复用已保存检索检查点，不重复查询向量")
                outcome = self.rag.answer(job["document_id"], job["question"], snapshot=snapshot,
                                          observer=observer, retrieval_checkpoint=retrieval)
                answer = RagAnswer.model_validate(outcome.answer).model_dump()
                # 正常返回的内容均已走 RAG 严格校验，不保存上游原始模型响应。
                if not self.store.save_checkpoint(job_id, token, role="answer", payload=answer,
                        sequence_no=current_sequence, elapsed_ms=_elapsed(call_started),
                        provider_usage=outcome.diagnostics.get("provider_usage"), diagnostics=outcome.diagnostics):
                    raise _Stopped("lease_lost", "执行租约已失效，已校验回答未保存")
                current_sequence, call_started = None, None
            progress("saving", "正在保存已校验回答和来源")
            if not self.store.publish(job_id, token, answer):
                check_active()
                raise _Stopped("publish_rejected", "问答结果未发布，请查看任务状态")
            logger.info("问答任务 %s request_id=%s 已完成", job_id, job["request_id"])
        except _Stopped as exc:
            self.store.finish(job_id, token, status=exc.status, code=exc.code, message=exc.message,
                              call_status="uncertain" if current_sequence is not None else "failed",
                              elapsed_ms=_elapsed(call_started) if call_started is not None else None, provider_usage=provider_usage())
        except (ModelError, EmbeddingError) as exc:
            # 适配器异常虽然是安全提示，统一返回稳定状态，避免测试或新适配器夹带原始响应。
            uncertain = exc.status_code == 504
            code = "call_uncertain" if uncertain else "generation_error" if isinstance(exc, ModelError) else "embedding_error"
            self.store.finish(job_id, token, status="needs_attention" if uncertain else "failed", code=code,
                message=("上游请求超时，调用结果不确定；系统不会自动重发，请核实后明确重试" if uncertain else
                         "问答上游请求失败，系统不会自动重发；请检查模型配置或服务状态"),
                call_status="uncertain" if uncertain else "failed", elapsed_ms=_elapsed(call_started), provider_usage=provider_usage())
            logger.warning("问答任务 %s request_id=%s 失败 code=%s", job_id, job["request_id"], code)
        except RagError as exc:
            self.store.finish(job_id, token, status="failed", code=exc.code,
                message=("模型输出未通过结构或引用校验，未发布回答" if exc.code == "rag_output_invalid" else
                         "问答证据、输入范围或模型配置校验失败，未发布回答"), elapsed_ms=_elapsed(call_started), provider_usage=provider_usage())
            logger.warning("问答任务 %s request_id=%s 失败 code=%s", job_id, job["request_id"], exc.code)
        except Exception:
            uncertain = current_sequence is not None
            code = "call_uncertain" if uncertain else "local_processing_error"
            self.store.finish(job_id, token, status="needs_attention" if uncertain else "failed", code=code,
                message=("调用结果不确定，系统不会自动重发；请核实后明确重试" if uncertain else
                         "本地问答处理失败，已保存检查点和历史结果保留"),
                call_status="uncertain" if uncertain else "failed", elapsed_ms=_elapsed(call_started), provider_usage=provider_usage())
            logger.error("问答任务 %s request_id=%s 失败 code=%s", job_id, job["request_id"], code)


def _elapsed(started: float | None) -> int:
    return max(0, round((time.perf_counter() - started) * 1000)) if started is not None else 0
