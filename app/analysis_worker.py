"""分析 worker：从 SQLite 领取摘要／提取任务，分批生成、校验、汇总并发布结果。

运行方式：``python -m app.analysis_worker``

设计要点（对应任务书 §5、§6、§7）：

1. **SQLite 是任务事实来源**。Web 进程只创建任务与查询结果，不执行生成；
   worker 使用条件更新原子领取，记录执行令牌与租约，续租与最终发布都校验令牌。
2. **每次外部请求前先预扣预算并写调用意图**（同一短事务）。若进程在“已发出请求、
   尚未保存结果”时中断，账本里会残留 intent 行；重启后该任务转 needs_attention，
   **绝不自动重发**，因为供应商是否已接收无法确定。
3. **已校验步骤直接复用**。重启或明确重试只做未完成的批次，已成功批次不再调用模型。
4. **中间摘要不是原文**。汇总阶段接收已校验条目及全部完整引述，最终引用沿链回落
   到原文单元编号；无法回落时不会发布“完整全文摘要”。
5. **失败不丢旧结果**。发布只在全部必需步骤完成后进行；失败或取消时旧结果继续可读。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from threading import Event, Thread

from app import extraction as extraction_module
from app import summarization as summarization_module
from app.analysis_jobs import KIND_VERSIONS, analysis_model_signature
from app.analysis_sources import (
    AnalysisPlanResult,
    InputUnit,
    build_plan,
    relevant_warnings,
)
from app.analysis_validation import AnalysisOutputError
from app.config import Settings
from app.deepseek import ModelError
from app.repository import Repository
from app.schemas import AnalysisJob, QualityWarning

logger = logging.getLogger("docqa.analysis")

# 稳定错误码：任务失败原因只暴露这些码与安全中文提示。
ERROR_PLAN_CHANGED = "plan_changed"
ERROR_BUDGET_EXHAUSTED = "budget_exhausted"
ERROR_OUTPUT_INVALID = "output_invalid"
ERROR_MODEL = "model_error"
ERROR_LOCAL_IO = "local_io_error"
ERROR_ITEMS_TOO_MANY = "items_too_many"
ERROR_CANCELLED = "cancelled"
ERROR_INCOMPLETE = "incomplete_coverage"
ERROR_CALL_UNCERTAIN = "call_uncertain"


class AnalysisWorker:
    """串行处理分析任务的 worker；模型可注入，便于离线测试与评估脚本复用。"""

    def __init__(self, settings: Settings, repository: Repository | None = None, *,
                 model=None, worker_id: str | None = None, sleep=time.sleep):
        self.settings = settings
        self.repository = repository or Repository(settings.data_dir / "docqa.db")
        if model is None:
            from app.deepseek import DeepSeekModel

            model = DeepSeekModel(settings)
        self.model = model
        self.worker_id = worker_id or f"{os.getpid()}-{int(time.time())}"
        self.sleep = sleep
        self.stop_requested = False
        # 当前正在执行任务的输入计划：只用于失败时给出真实的覆盖口径。
        self._active_plan: AnalysisPlanResult | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def request_stop(self, *_args) -> None:
        """SIGINT/SIGTERM 处理：只设置标记，让当前任务留下可恢复状态后退出。"""
        self.stop_requested = True
        logger.warning("收到停止请求：当前任务将保留可恢复状态后退出")

    def run_forever(self, *, max_tasks: int | None = None) -> int:
        processed = 0
        self.repository.initialize()
        while not self.stop_requested:
            job = self.repository.claim_next_analysis_job(self.worker_id,
                                                        self.settings.worker_lease_seconds)
            if job is None:
                if max_tasks is not None:
                    break
                self.sleep(self.settings.worker_idle_sleep_seconds)
                continue
            self.process_job(job)
            processed += 1
            if max_tasks is not None and processed >= max_tasks:
                break
        return processed

    def run_job(self, job_id: str) -> bool:
        """显式执行指定任务（测试与手动恢复使用）。"""
        job = self.repository.claim_next_analysis_job(self.worker_id,
                                                     self.settings.worker_lease_seconds,
                                                     job_id=job_id)
        if job is None:
            logger.info("分析任务 %s 当前不可领取（可能已完成或由其他 worker 持有）", job_id)
            return False
        self.process_job(job)
        return True

    def process_job(self, job: AnalysisJob) -> None:
        """心跳覆盖整个生成过程，避免长请求期间租约过期被其他 worker 接管。"""
        done = Event()

        def heartbeat():
            while not done.wait(max(0.2, self.settings.worker_lease_seconds / 3)):
                try:
                    if not self.repository.renew_analysis_lease(
                            job.id, job.lease_token or "", self.settings.worker_lease_seconds):
                        return
                except Exception:
                    logger.error("分析任务 %s 续租失败，后续发布仍须通过租约检查", job.id)
                    return

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self._process_job(job)
        finally:
            done.set()
            thread.join(timeout=2)

    # ------------------------------------------------------------------
    # 单任务处理
    # ------------------------------------------------------------------
    def _process_job(self, job: AnalysisJob) -> None:
        token = job.lease_token or ""
        self._active_plan = None
        try:
            if self.repository.analysis_cancel_requested(job.id):
                self.repository.fail_analysis_job(
                    job.id, token, status="cancelled", stage="done", error_code=ERROR_CANCELLED,
                    error_message="任务在开始执行前被取消，未产生任何模型调用")
                return
            if self.repository.has_uncertain_analysis_call(job.id):
                # 存在未确认的调用：不自动重发，停下等待用户核实。
                self.repository.fail_analysis_job(
                    job.id, token, status="needs_attention", stage=job.stage,
                    error_code=ERROR_CALL_UNCERTAIN,
                    error_message="有一次已发出的生成调用未保存结果，供应商是否已接收无法确定；"
                                  "系统不会自动重发，请核实后明确重试（可能重复计费）")
                return
            plan = self._rebuild_plan(job)
            if plan is None:
                return
            self._active_plan = plan
            self.repository.set_analysis_plan_counts(
                job.id, token, steps_total=len(plan.batches) + (1 if plan.reduce_required else 0),
                request_upper_bound=plan.request_upper_bound)
            self.repository.update_analysis_progress(job.id, token, stage="generating",
                                                     stage_detail="正在按批次生成并校验")
            if job.kind == "extraction":
                self._run_extraction(job, token, plan)
            else:
                self._run_summary(job, token, plan)
        except ModelError as exc:
            # 上游错误只映射为安全提示；已写意图的调用已由结算逻辑标记 failed。
            self.repository.fail_analysis_job(
                job.id, token, status="failed", stage="done", error_code=ERROR_MODEL,
                error_message=str(exc))
        except AnalysisOutputError as exc:
            logger.warning("分析任务 %s 输出校验失败：reason=%s", job.id, exc.reason)
            self.repository.fail_analysis_job(
                job.id, token, status="failed", stage="done",
                error_code=f"{ERROR_OUTPUT_INVALID}:{exc.reason}",
                error_message="模型输出未通过结构或引用校验；本次不会返回未校验内容，"
                              "已校验的批次检查点保留，可在剩余预算内明确重试",
                coverage=self._active_plan_coverage(job.id))
        except Exception:  # noqa: BLE001 - worker 必须把任意异常转换为任务终态
            # 未分类异常可能夹带路径、凭证或原始响应，不记录异常正文和堆栈。
            logger.error("分析任务 %s 本地处理失败", job.id)
            self.repository.fail_analysis_job(
                job.id, token, status="failed", stage="done", error_code=ERROR_LOCAL_IO,
                error_message="本地处理失败，请检查服务日志后重试；已有成功结果不受影响",
                coverage=self._active_plan_coverage(job.id))

    def _active_plan_coverage(self, job_id: str) -> dict | None:
        """当前计划下的已达覆盖口径；没有计划可用时返回 None（保持已有覆盖字段）。

        用途：任何失败路径都必须如实显示“已完成多少批次、还有多少单元没处理”，
        而不是沿用创建任务时的“0 处理”，也不能因为失败就清空已有进度。
        """
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            return None
        return self._progress_coverage(plan, 0, job_id)

    # ------------------------------------------------------------------
    # 计划重建
    # ------------------------------------------------------------------
    def _rebuild_plan(self, job: AnalysisJob) -> AnalysisPlanResult | None:
        """按**任务创建时固定的解析版本**重建输入计划，并核对计划指纹。

        计划在创建时已固定；若解析版本内容或预算配置发生变化导致指纹不一致，
        本次不执行：避免用新输入生成旧任务结果，也避免静默扩大费用。
        """
        version = self.repository.get_parse_version(job.parse_version_id)
        document = self.repository.get(job.document_id)
        token = job.lease_token or ""
        if job.model_signature != analysis_model_signature(self.settings):
            self.repository.fail_analysis_job(
                job.id, token, status="failed", stage="done", error_code=ERROR_PLAN_CHANGED,
                error_message="模型配置与任务快照不一致；请恢复原配置或重新查看计划后新建任务")
            return None
        if version is None or document is None:
            self.repository.fail_analysis_job(
                job.id, token, status="failed", stage="done",
                error_code=ERROR_LOCAL_IO, error_message="解析版本或文档记录不存在")
            return None
        blocks = self.repository.blocks(job.parse_version_id)
        prompt_version, _ = KIND_VERSIONS[job.kind]
        system_prompt = (extraction_module.extraction_system_prompt()
                         if job.kind == "extraction"
                         else summarization_module.summary_system_prompt())
        plan = build_plan(kind=job.kind, blocks=blocks, document_name=document.filename,
                          version_id=job.parse_version_id, settings=self.settings,
                          prompt_version=prompt_version,
                          system_prompt_chars=len(system_prompt),
                          parse_quality_status=version.quality_status,
                          parse_is_legacy=version.is_legacy)
        if not plan.executable:
            self.repository.fail_analysis_job(
                job.id, token, status="failed", stage="done",
                error_code=f"plan_blocked:{plan.blocked_reason}",
                error_message="；".join(plan.limitations) or "输入计划不可执行")
            return None
        if job.plan_fingerprint and plan.fingerprint != job.plan_fingerprint:
            self.repository.fail_analysis_job(
                job.id, token, status="failed", stage="done",
                error_code=ERROR_PLAN_CHANGED,
                error_message="解析版本内容或预算配置在任务创建后发生变化，"
                              "本次不执行以避免用新输入生成旧任务结果；请重新查看分析范围后新建任务")
            return None
        return plan

    # ------------------------------------------------------------------
    # 提取
    # ------------------------------------------------------------------
    def _run_extraction(self, job: AnalysisJob, token: str, plan: AnalysisPlanResult) -> None:
        document = self.repository.get(job.document_id)
        checkpoints = self.repository.succeeded_analysis_steps(job.id)
        batches: list[extraction_module.ValidatedExtraction] = []
        batch_units: list[list[InputUnit]] = []
        total_items = 0
        for index, units in enumerate(plan.batches):
            batch_id = f"b{index + 1}"
            units_with_ids = _units_with_local_ids(units)
            step = checkpoints.get(("batch", index))
            if step is not None:
                # 已校验检查点：直接复用，不重复发请求。
                batches.append(_extraction_from_payload(step["payload"]))
                batch_units.append(units_with_ids)
                total_items += len(step["payload"].get("items", []))
                if total_items > self.settings.analysis_max_items_total:
                    raise AnalysisOutputError("items_too_many", "检查点总条目数超过上限")
                logger.info("分析任务 %s 复用已校验批次 %s", job.id, batch_id)
                continue
            if self.repository.analysis_cancel_requested(job.id):
                self._finish_cancelled(job, token, plan, len(batches))
                return
            request = extraction_module.build_batch_request(
                units=units_with_ids, document_name=document.filename,
                version_id=job.parse_version_id, batch_index=index + 1,
                batch_total=len(plan.batches))

            def step_payload(raw: str, _units=units_with_ids, _batch_id=batch_id):
                """校验一批提取并返回检查点 payload；批内编号与引述一并保存。"""
                validated = extraction_module.validate_batch(
                    raw, units=_units, batch_id=_batch_id,
                    max_items=self.settings.analysis_max_items_per_batch)
                return validated, _extraction_checkpoint(validated)

            validated, failure = self._run_batch_call(
                job, token, role="batch", step_index=index, batch_id=batch_id,
                system_prompt=request.system_prompt, user_prompt=request.user_prompt,
                step_payload=step_payload)
            if failure is not None:
                self._fail_from_batch(job, token, plan, len(batches), failure)
                return
            total_items += len(validated.items)
            if total_items > self.settings.analysis_max_items_total:
                # 超量受控失败：不静默截断尾部条目再宣称完整。
                self.repository.fail_analysis_job(
                    job.id, token, status="failed", stage="done",
                    error_code=ERROR_ITEMS_TOO_MANY,
                    error_message=f"提取条目总数超过上限 {self.settings.analysis_max_items_total}；"
                                  "本次不截断结果，请缩小分析范围或调大上限后重试",
                    coverage=self._progress_coverage(plan, len(batches) + 1, job.id))
                return
            batches.append(validated)
            batch_units.append(units_with_ids)

        # 确定性合并：来源编号显式映射，跨批相同数字不视为同一来源。
        lookup = _unit_lookup(batch_units)
        for index, batch in enumerate(batches):
            lookup.remember(index, batch.quotes)  # type: ignore[attr-defined]
        items, citations = extraction_module.merge_batches(batches, unit_lookup=lookup)
        sections = extraction_module.section_statuses(items, batches)
        payload = {
            "items": [_extraction_item_payload(item) for item in items],
            "sections": sections,
            "citations": citations,
        }
        coverage = _coverage(plan, len(plan.batches), reduce_completed=False, complete=True,
                             citations=citations)
        limitations = extraction_module.collect_limitations(batches) + list(plan.limitations)
        self._publish(job, token, plan, payload=payload, coverage=coverage,
                      limitations=limitations, kind="extraction")

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------
    def _run_summary(self, job: AnalysisJob, token: str, plan: AnalysisPlanResult) -> None:
        document = self.repository.get(job.document_id)
        checkpoints = self.repository.succeeded_analysis_steps(job.id)
        batches: list[summarization_module.ValidatedSummaryBatch] = []
        batch_units: list[list[InputUnit]] = []
        for index, units in enumerate(plan.batches):
            batch_id = f"b{index + 1}"
            units_with_ids = _units_with_local_ids(units)
            step = checkpoints.get(("batch", index))
            if step is not None:
                batches.append(_summary_batch_from_payload(step["payload"]))
                batch_units.append(units_with_ids)
                logger.info("分析任务 %s 复用已校验摘要批次 %s", job.id, batch_id)
                continue
            if self.repository.analysis_cancel_requested(job.id):
                self._finish_cancelled(job, token, plan, len(batches))
                return
            request = summarization_module.build_batch_request(
                units=units_with_ids, document_name=document.filename,
                version_id=job.parse_version_id, batch_index=index + 1,
                batch_total=len(plan.batches))

            def step_payload(raw: str, _units=units_with_ids, _batch_id=batch_id):
                validated = summarization_module.validate_batch(raw, units=_units,
                                                                batch_id=_batch_id)
                return validated, _summary_checkpoint(validated)

            validated, failure = self._run_batch_call(
                job, token, role="batch", step_index=index, batch_id=batch_id,
                system_prompt=request.system_prompt, user_prompt=request.user_prompt,
                step_payload=step_payload)
            if failure is not None:
                self._fail_from_batch(job, token, plan, len(batches), failure)
                return
            batches.append(validated)
            batch_units.append(units_with_ids)

        limitations = _dedupe([text for batch in batches for text in batch.limitations]
                              + list(plan.limitations))
        # 批内编号 -> 原文引用的解析器：引述来自各批已校验检查点。
        entry_lookup = _entry_lookup(batch_units)
        for index, batch in enumerate(batches):
            entry_lookup.remember(index, batch.quotes)  # type: ignore[attr-defined]

        if not plan.reduce_required:
            # 单批摘要：批内编号直接映射到原文引用，不产生额外计费调用。
            batch = batches[0]
            # 两个分区必须共用编号映射，否则例外的 [1] 会误指向第一条要点。
            points, citations = summarization_module.resolve_direct_points(
                batch.main_points + batch.exceptions, batch=batch)
            main = points[:len(batch.main_points)]
            exceptions = points[len(batch.main_points):]
            # 用服务端映射补齐来源定位：块 ID、来源下标、引述偏移全部来自本次输入，
            # 模型既看不到也无法伪造；缺失定位时如实保留空值而不编造页码。
            citations = [_enrich_citation(citation, entry_lookup, 0) for citation in citations]
            payload = {
                "topic_overview": batch.topic_overview,
                "main_points": [{"text": p.text, "refs": p.refs} for p in main],
                "exceptions": [{"text": p.text, "refs": p.refs} for p in exceptions],
                "limitations": limitations,
                "citations": citations,
            }
            coverage = _coverage(plan, len(plan.batches), reduce_completed=False, complete=True,
                                 citations=citations)
            self._publish(job, token, plan, payload=payload, coverage=coverage,
                          limitations=limitations, kind="summary")
            return

        reduce_step = checkpoints.get(("reduce", 0))
        if reduce_step is not None:
            # 汇总检查点已保存最终结构：直接发布，不重新调用模型。
            payload = reduce_step["payload"]
            coverage = _coverage(plan, len(plan.batches), reduce_completed=True, complete=True,
                                 citations=payload.get("citations", []))
            self._publish(job, token, plan, payload=payload, coverage=coverage,
                          limitations=payload.get("limitations", []), kind="summary")
            return

        entries: list[dict] = []
        for index, batch in enumerate(batches):
            entries.extend(summarization_module.batch_entries(
                index, f"b{index + 1}", batch, entry_lookup))

        if self.repository.analysis_cancel_requested(job.id):
            self._finish_cancelled(job, token, plan, len(batches))
            return
        request = summarization_module.build_reduce_request(
            document_name=document.filename, version_id=job.parse_version_id,
            batch_total=len(batches), entries=entries)
        self.repository.update_analysis_progress(job.id, token, stage="reducing",
                                                 stage_detail="正在汇总各批已校验中间结果")

        def reduce_payload(raw: str, _entries=entries, _limitations=limitations):
            """校验汇总输出并把 `item_id` 引用沿链回落到原文单元编号。"""
            reduced = summarization_module.validate_reduce_output(raw, entries=_entries)
            main, exceptions, citations = summarization_module.resolve_reduce_points(
                reduced, entries=_entries)
            return reduced, {
                "topic_overview": reduced.topic_overview,
                "main_points": [{"text": p.text, "refs": p.refs} for p in main],
                "exceptions": [{"text": p.text, "refs": p.refs} for p in exceptions],
                "limitations": _dedupe(_limitations + reduced.limitations),
                "citations": citations,
            }

        _validated, failure = self._run_batch_call(
            job, token, role="reduce", step_index=0, batch_id=None,
            system_prompt=request.system_prompt, user_prompt=request.user_prompt,
            step_payload=reduce_payload)
        if failure is not None:
            self._fail_from_batch(job, token, plan, len(batches), failure)
            return
        saved = self.repository.succeeded_analysis_steps(job.id).get(("reduce", 0))
        if saved is None:
            # 汇总未落盘：不发布“完整全文摘要”，但保留已校验批次供明确重试。
            self.repository.fail_analysis_job(
                job.id, token, status="failed", stage="done", error_code=ERROR_INCOMPLETE,
                error_message="汇总步骤未保存成功，本次不发布完整摘要；已校验批次检查点保留",
                coverage=self._progress_coverage(plan, len(batches), job.id))
            return
        payload = saved["payload"]
        coverage = _coverage(plan, len(plan.batches), reduce_completed=True, complete=True,
                             citations=payload.get("citations", []))
        self._publish(job, token, plan, payload=payload, coverage=coverage,
                      limitations=payload.get("limitations", []), kind="summary")

    # ------------------------------------------------------------------
    # 单批调用：预扣预算 → 写意图 → 请求 → 校验 → 保存检查点 → 结算
    # ------------------------------------------------------------------
    def _run_batch_call(self, job: AnalysisJob, token: str, *, role: str, step_index: int,
                        batch_id: str | None, system_prompt: str, user_prompt: str,
                        step_payload):
        """执行一次生成调用，返回 (已校验结果, 失败信息)。

        `step_payload` 是调用方提供的回调：接收模型原始正文，返回
        (已校验结果, 检查点 payload)。校验与检查点构造由调用方决定，
        因而提取、摘要批次与汇总各自保留自己的协议，而预算与账本顺序完全一致。

        失败信息为 (error_code, message, is_uncertain)：
        - 预算耗尽或租约失效时**不会**发起请求，错误码为 budget_exhausted；
        - 请求发出后本地未保存结果就中断的情形由账本 intent 行表达，
          由重启恢复逻辑转 needs_attention，这里绝不自动重发。
        """
        step_id = f"astep-{job.id}-{role}-{step_index}"
        if self.stop_requested:
            return None, ("worker_stopped", "执行者已停止；已校验检查点保留，可明确重试", False)
        limit = (min(self.settings.analysis_reduce_max_chars, self.settings.analysis_input_max_chars)
                 if role == "reduce" else self.settings.analysis_input_max_chars)
        if len(system_prompt) + len(user_prompt) > limit:
            return None, ("input_budget_exceeded", "完整请求超过输入预算，未发起新的请求", False)
        sequence = self.repository.claim_analysis_budget(job.id, token, role=role,
                                                        step_id=step_id)
        if sequence is None:
            return None, (ERROR_BUDGET_EXHAUSTED,
                          "本次任务的生成预算已用尽或执行令牌已失效；未发起新的请求", False)
        started = time.perf_counter()
        try:
            raw = self.model.generate(system_prompt, user_prompt)
        except ModelError as exc:
            self.repository.settle_analysis_call(job.id, sequence, status="failed",
                                                error_code=ERROR_MODEL,
                                                elapsed_ms=_elapsed_ms(started))
            return None, (ERROR_MODEL, str(exc), False)
        except Exception:
            # 未分类异常：调用可能已在供应商侧发生，账本保持 intent，
            # 交由恢复逻辑转 needs_attention，绝不自动重发。
            logger.error("分析任务 %s 的生成调用出现未分类异常，结果不确定", job.id)
            return None, (ERROR_CALL_UNCERTAIN,
                          "生成调用结果不确定，系统不会自动重发；请核实后明确重试", True)
        try:
            validated, payload = step_payload(raw)
        except AnalysisOutputError as exc:
            self.repository.settle_analysis_call(job.id, sequence, status="failed",
                                                error_code=f"{ERROR_OUTPUT_INVALID}:{exc.reason}",
                                                elapsed_ms=_elapsed_ms(started))
            raise
        # 先保存已校验检查点，再结算调用：顺序颠倒会在“保存前崩溃”时
        # 产生 intent 遗留，从而正确地进入 needs_attention，而不是被当成成功。
        saved = self.repository.save_analysis_step(
            job.id, token, step_id=step_id, role=role, order_index=step_index,
            batch_id=batch_id, unit_ids=list(getattr(validated, "unit_ids", []) or []),
            input_chars=len(user_prompt), payload=payload)
        if not saved:
            return None, ("lease_lost", "执行令牌已失效，本次结果未保存", False)
        self.repository.settle_analysis_call(job.id, sequence, status="succeeded",
                                            elapsed_ms=_elapsed_ms(started))
        logger.info("分析任务 %s 完成 %s 步骤 %s（第 %s 次调用）", job.id, role, step_index, sequence)
        return validated, None

    def _fail_from_batch(self, job: AnalysisJob, token: str, plan: AnalysisPlanResult,
                         completed: int, failure: tuple[str, str, bool]) -> None:
        """批次失败：写入稳定失败状态，并保留可显示的已完成进度。"""
        code, message, uncertain = failure
        if self.repository.analysis_cancel_requested(job.id):
            self._finish_cancelled(job, token, plan, completed)
            return
        if uncertain:
            self.repository.fail_analysis_job(
                job.id, token, status="needs_attention", stage="generating",
                error_code=ERROR_CALL_UNCERTAIN,
                error_message=message + "（供应商是否已接收无法确定，可能重复计费）",
                coverage=self._progress_coverage(plan, completed, job.id))
            return
        status = "needs_attention" if code == ERROR_BUDGET_EXHAUSTED else "failed"
        self.repository.fail_analysis_job(
            job.id, token, status=status, stage="done", error_code=code, error_message=message,
            coverage=self._progress_coverage(plan, completed, job.id))

    def _finish_cancelled(self, job: AnalysisJob, token: str, plan: AnalysisPlanResult,
                          completed: int) -> None:
        """取消：不发布成功结果，保留已完成批次的进度供页面显示。"""
        self.repository.fail_analysis_job(
            job.id, token, status="cancelled", stage="done", error_code=ERROR_CANCELLED,
            error_message="已取消后续处理；已在途的生成请求可能继续产生费用，"
                          "系统不承诺供应商停止计费。已校验的批次检查点保留",
            coverage=self._progress_coverage(plan, completed, job.id))

    def _progress_coverage(self, plan: AnalysisPlanResult, completed: int,
                           job_id: str | None = None) -> dict:
        """未完成时的覆盖口径：完整声明“哪些单元没有被处理及原因”。

        `completed` 只作为兜底；实际完成批次数量以**已校验检查点**为准，
        因为失败批次本身不会留下 succeeded 行，用“失败批次下标”会高估进度。
        """
        if job_id is not None:
            succeeded = sum(1 for (role, _index) in self.repository.succeeded_analysis_steps(job_id)
                            if role == "batch")
            completed = max(completed, succeeded)
        processed = sum(len(batch) for batch in plan.batches[:completed])
        unresolved = len(plan.units) - processed
        return {
            "total_units": len(plan.units),
            "processed_units": processed,
            "unresolved_units": unresolved,
            "unresolved_reasons": ({"batch_not_completed": unresolved} if unresolved else {}),
            "excluded_units": plan.excluded_units,
            "excluded_reasons": dict(plan.excluded_counts),
            "batch_total": len(plan.batches),
            "batch_completed": completed,
            "reduce_completed": False,
            "complete": False,
            "resolved_original_refs": 0,
            "unresolved_original_refs": 0,
        }

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def _publish(self, job: AnalysisJob, token: str, plan: AnalysisPlanResult, *, payload: dict,
                 coverage: dict, limitations: list[str], kind: str) -> None:
        """发布已校验结果：所有必需批次都已完成（含汇总）才会走到这里。

        结果正文与结果行在同一短事务中写入，因此重启后读取结果不需要再调用模型，
        也不会出现“结果行存在但正文缺失”的半完成状态。
        """
        version = self.repository.get_parse_version(job.parse_version_id)
        warnings = _presentable(relevant_warnings(version.warnings if version else [], plan.units))
        limitations = _dedupe([text for text in limitations if text])
        self.repository.update_analysis_progress(job.id, token, stage="saving",
                                                 stage_detail="正在保存已校验结果")
        prompt_version, protocol_version = KIND_VERSIONS[kind]
        published = self.repository.publish_analysis_result(
            job.id, token, result_id=f"res-{job.id}", kind=kind, payload=payload,
            coverage=coverage, warnings=warnings, limitations=limitations,
            prompt_version=prompt_version, protocol_version=protocol_version,
            model_signature=analysis_model_signature(self.settings),
            requests_used=self.repository.analysis_calls_used(job.id))
        if not published:
            if self.repository.analysis_cancel_requested(job.id):
                self._finish_cancelled(job, token, plan, len(plan.batches))
                return
            logger.warning("分析任务 %s 发布时令牌失效，本次结果未发布", job.id)
            return
        logger.info("分析任务 %s 完成并发布结果 res-%s：覆盖 %s/%s",
                    job.id, job.id, coverage["processed_units"], coverage["total_units"])


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _units_with_local_ids(units: list[InputUnit]) -> list[InputUnit]:
    """为一批单元重新分配批内编号（1..n）。

    编号**只在本批内有效**：这样模型不可能把另一批的 3 号当成本批的 3 号。
    """
    copied: list[InputUnit] = []
    for position, unit in enumerate(units, start=1):
        copied.append(InputUnit(
            unit_id=unit.unit_id, batch_local_id=position, block_id=unit.block_id,
            order_index=unit.order_index, kind=unit.kind, block_type=unit.block_type,
            text=unit.text, heading_path=unit.heading_path, sources=list(unit.sources),
            source_ids=list(unit.source_ids)))
    return copied


def _entry_for(batch_index: int, ref: int, batch_units: list[list[InputUnit]],
               quote: str) -> dict:
    """把一个批内编号解析为结构化原文引用（块 ID、来源下标、引述与偏移）。

    这里**只使用服务端持有的映射**：模型提供的任何标识都不参与定位，
    因此引用卡片上的块 ID、页码与单元格范围不可能被伪造。
    """
    units = batch_units[batch_index]
    unit = next((item for item in units if item.batch_local_id == ref), None)
    if unit is None:
        return {"block_id": "", "source_index": 0, "quote": quote, "sources": [],
                "char_start": None, "char_end": None, "block_type": None}
    normalized = unit.text.replace("\r\n", "\n")
    offset = normalized.find(quote)
    return {
        "block_id": unit.block_id,
        "source_index": unit.source_ids[0] if unit.source_ids else 0,
        "quote": quote,
        "sources": [source.model_dump() for source in unit.sources],
        "char_start": offset if offset >= 0 else None,
        "char_end": (offset + len(quote)) if offset >= 0 else None,
        "block_type": unit.block_type,
    }


def _unit_lookup(batch_units: list[list[InputUnit]]):
    """返回“第几批的第几号 → 原文引用”的解析函数。

    调用方必须先 `remember(batch_index, quotes)` 写入该批已校验的引述；
    未登记的批次会解析出空引述（并被覆盖统计记录为未回落引用），
    绝不猜测或补造引述。

    返回的条目同时带 `ref`（批内编号）供合并去重使用：合并键需要“来源身份”，
    而来源身份由块 ID、来源下标与引述共同确定。
    """
    state: dict[int, dict[int, str]] = {}

    def remember(batch_index: int, quotes: dict[int, str]) -> None:
        state[batch_index] = quotes

    def lookup(batch_index: int, ref: int) -> dict:
        entry = _entry_for(batch_index, ref, batch_units,
                           state.get(batch_index, {}).get(ref, ""))
        return {"ref": ref, **entry}

    lookup.remember = remember  # type: ignore[attr-defined]
    return lookup


# 摘要批次的条目解析与提取共用同一映射函数（引述同样来自已校验检查点）。
_entry_lookup = _unit_lookup


def _extraction_checkpoint(validated) -> dict:
    """提取批次的检查点 payload（只保存已校验结构）。"""
    return {
        "items": [_extraction_item_payload(item) for item in validated.items],
        "quotes": {str(ref): quote for ref, quote in validated.quotes.items()},
        "sections": validated.sections,
        "limitations": validated.limitations,
    }


def _summary_checkpoint(validated) -> dict:
    """摘要批次的检查点 payload。"""
    return {
        "topic_overview": validated.topic_overview,
        "main_points": [{"text": p.text, "refs": p.refs} for p in validated.main_points],
        "exceptions": [{"text": p.text, "refs": p.refs} for p in validated.exceptions],
        "quotes": {str(ref): quote for ref, quote in validated.quotes.items()},
        "limitations": validated.limitations,
    }


def _extraction_item_payload(item) -> dict:
    return {
        "item_id": item.item_id, "kind": item.kind, "content": item.content,
        "name": item.name, "value_text": item.value_text, "unit": item.unit,
        "period": item.period, "subject": item.subject, "scope": item.scope,
        "refs": list(item.refs),
    }


def _extraction_from_payload(payload: dict):
    from app.analysis_validation import ValidatedExtraction, ValidatedItem

    items = [ValidatedItem(item_id=item["item_id"], kind=item["kind"], content=item["content"],
                           name=item.get("name"), value_text=item.get("value_text"),
                           unit=item.get("unit"), period=item.get("period"),
                           subject=item.get("subject"), scope=item.get("scope"),
                           refs=list(item.get("refs", [])))
             for item in payload.get("items", [])]
    return ValidatedExtraction(
        items=items,
        quotes={int(ref): quote for ref, quote in (payload.get("quotes") or {}).items()},
        sections=payload.get("sections", {}),
        limitations=payload.get("limitations", []))


def _summary_batch_from_payload(payload: dict):
    from app.analysis_validation import ValidatedSummaryBatch, ValidatedSummaryPoint

    return ValidatedSummaryBatch(
        topic_overview=payload.get("topic_overview", ""),
        main_points=[ValidatedSummaryPoint(text=item["text"], refs=list(item["refs"]))
                     for item in payload.get("main_points", [])],
        exceptions=[ValidatedSummaryPoint(text=item["text"], refs=list(item["refs"]))
                    for item in payload.get("exceptions", [])],
        quotes={int(ref): quote for ref, quote in (payload.get("quotes") or {}).items()},
        limitations=payload.get("limitations", []))


def _enrich_citation(citation: dict, lookup, batch_index: int) -> dict:
    """用服务端映射补齐引用的来源定位（块 ID、来源下标、偏移与来源列表）。

    `lookup(batch_index, ref)` 返回的定位完全来自本次输入单元；若该编号没有
    对应单元（理论上不会发生），保留空定位而不是编造页码或坐标。
    """
    ref = citation.get("ref")
    if ref is None:
        return citation
    entry = lookup(batch_index, ref)
    enriched = {key: value for key, value in citation.items() if key != "ref"}
    enriched.setdefault("quote", entry.get("quote", ""))
    for key in ("block_id", "source_index", "sources", "char_start", "char_end", "block_type"):
        # 定位缺失时保持空值，不编造页码或坐标；block_id 缺失时用空串表示不可定位。
        value = entry.get(key)
        enriched[key] = "" if (key == "block_id" and value is None) else value
    enriched["source_index"] = enriched.get("source_index")
    return enriched


def _renumber_citations(first: list[dict], second: list[dict]) -> list[dict]:
    """把两组引用合并为连续编号，避免要点与例外使用两套编号空间。"""
    index: dict[tuple, int] = {}
    merged: list[dict] = []
    for citation in list(first) + list(second):
        key = (citation.get("block_id"), citation.get("source_index"), citation.get("quote"),
               citation.get("ref"))
        if key in index:
            continue
        index[key] = len(merged) + 1
        merged.append({**citation, "reference_id": index[key]})
    return merged


def _coverage(plan: AnalysisPlanResult, batch_completed: int, *, reduce_completed: bool,
              complete: bool, citations: list[dict]) -> dict:
    """构造覆盖口径，并记录最终引用是否全部回落到原文单元。"""
    processed = sum(len(batch) for batch in plan.batches)
    unresolved = len(plan.units) - processed
    resolved = sum(1 for citation in citations if citation.get("block_id"))
    return {
        "total_units": len(plan.units),
        "processed_units": processed,
        "unresolved_units": unresolved,
        "unresolved_reasons": ({"unit_not_sent_to_model": unresolved} if unresolved else {}),
        "excluded_units": plan.excluded_units,
        "excluded_reasons": dict(plan.excluded_counts),
        "batch_total": len(plan.batches),
        "batch_completed": batch_completed,
        "reduce_completed": reduce_completed,
        "complete": bool(complete and unresolved == 0),
        "resolved_original_refs": resolved,
        "unresolved_original_refs": len(citations) - resolved,
    }


def _presentable(warnings: list[QualityWarning]) -> list[QualityWarning]:
    """按告警码去重并排序：公式缓存、OCR 等与内容可靠性相关的限制优先展示。"""
    priority_keys = ("formula", "ocr", "equation", "chunking", "table", "source", "empty")
    deduped: dict[str, QualityWarning] = {}
    for warning in warnings:
        deduped.setdefault(warning.code, warning)

    def priority(warning: QualityWarning) -> tuple[int, str]:
        related = 0 if any(key in warning.code for key in priority_keys) else 1
        severity = {"error": 0, "warning": 1, "info": 2}.get(warning.severity, 3)
        return (related * 10 + severity, warning.code)

    return sorted(deduped.values(), key=priority)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DocQA 分析 worker：从 SQLite 领取并执行摘要／提取任务")
    parser.add_argument("--once", action="store_true", help="只处理当前可领取的任务后退出")
    parser.add_argument("--job", help="只执行指定分析任务 ID（用于手动恢复）")
    parser.add_argument("--max-tasks", type=int, default=None, help="最多处理多少个任务后退出")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    worker = AnalysisWorker(settings)
    signal.signal(signal.SIGINT, worker.request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, worker.request_stop)
    if args.job:
        worker.repository.initialize()
        worker.run_job(args.job)
        return 0
    max_tasks = 1 if args.once else args.max_tasks
    processed = worker.run_forever(max_tasks=max_tasks)
    logger.info("分析 worker 退出，本次处理 %s 个任务", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
