"""分析任务服务：输入规划、任务生命周期、结果读取与导出。

**本模块不做任何模型调用。** 生成只发生在 `app/analysis_worker.py` 中，由独立进程
从 SQLite 领取任务后执行。这样拆分的理由是任务书 5.1／5.3 的硬约束：

- 页面刷新、GET 状态查询、能力探测、规划、查看历史与导出都不得触发生成；
- 计划必须先展示给用户（范围、解析版本、请求上界、已有结果），再由用户明确发起；
- 创建任务的接口只写数据库，绝不“顺便”发一次调用。

规划结果是**创建任务时固定**的：计划指纹绑定解析版本、单元内容与顺序、分批结果和
相关预算。若提交时指纹与当前规划不一致（例如预览版本已切换或预算被调整），
接口返回 409 并要求重新展示计划，避免静默扩大费用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from app.analysis_prompts import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_PROTOCOL_VERSION,
    SUMMARY_PROMPT_VERSION,
    SUMMARY_PROTOCOL_VERSION,
    extraction_system_prompt,
    summary_system_prompt,
)
from app.analysis_sources import (
    EXCLUDE_REASONS,
    INPUT_PROTOCOL_VERSION,
    AnalysisPlanResult,
    build_plan,
)
from app.config import Settings
from app.rag_context import RagError
from app.repository import Repository, TaskConflict, now_iso
from app.schemas import (
    AnalysisCoverage,
    AnalysisCitation,
    AnalysisExtractionItem,
    AnalysisExtractionResult,
    AnalysisJob,
    AnalysisJobRequest,
    AnalysisPlan,
    AnalysisPlanBatch,
    AnalysisPlanCoverage,
    AnalysisPlanLimits,
    AnalysisResult,
    AnalysisResultSummary,
    AnalysisSummaryPoint,
    AnalysisSummaryResult,
    AnalysisSubmitResponse,
    Document,
    SourceLocation,
)

# 任务类型 -> Prompt 与协议版本。两类任务各自独立版本，互不冒充。
KIND_VERSIONS = {
    "extraction": (EXTRACTION_PROMPT_VERSION, EXTRACTION_PROTOCOL_VERSION),
    "summary": (SUMMARY_PROMPT_VERSION, SUMMARY_PROTOCOL_VERSION),
}


def analysis_model_signature(settings: Settings) -> str:
    """模型配置指纹（**不含密钥**）：用于判断同版本同配置能否复用已有成功结果。"""
    payload = {
        "base_url": settings.deepseek_base_url,
        "model": settings.deepseek_model,
        "thinking": settings.deepseek_thinking,
        "reasoning_effort": settings.deepseek_reasoning_effort,
        "max_tokens": settings.deepseek_max_tokens,
        "protocol": INPUT_PROTOCOL_VERSION,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def request_fingerprint(kind: str, version_id: str, plan_fingerprint: str, regenerate: bool = False) -> str:
    """创建请求的载荷指纹：同键不同载荷必须被识别为冲突。"""
    payload = f"kind={kind};version={version_id};plan={plan_fingerprint}"
    # 普通提交保持旧指纹兼容；主动重新生成属于不同有效载荷。
    if regenerate:
        payload += ";regenerate=true"
    return sha256(payload.encode()).hexdigest()


@dataclass
class AnalysisPlanOutcome:
    plan: AnalysisPlanResult
    version_id: str
    document: Document
    is_active_version: bool


class AnalysisJobService:
    """分析任务的查询与生命周期服务；生成逻辑在 worker 中。"""

    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository

    # ------------------------------------------------------------------
    # 前置检查与规划
    # ------------------------------------------------------------------
    def ready(self) -> tuple[bool, str]:
        """分析能力是否可用：只需要生成模型配置，**不需要** embedding 配置或索引。"""
        if not self.settings.deepseek_api_key.strip():
            return False, "摘要与提取需要先配置 DEEPSEEK_API_KEY 并重启服务"
        return True, ""

    def require_document(self, document_id: str) -> Document:
        document = self.repository.get(document_id)
        if document is None:
            raise RagError(404, "document_not_found", "文档不存在")
        return document

    def resolve_version(self, document: Document, version_id: str | None):
        """确定本次分析的解析版本：默认当前预览版本，可显式选择该文档的历史有效版本。"""
        target = version_id or document.active_parse_version_id
        if not target:
            raise RagError(409, "no_parse_version", "该文档尚无成功解析版本，请先完成解析")
        version = self.repository.get_parse_version(target)
        if version is None or version.document_id != document.id:
            raise RagError(404, "parse_version_not_found", "解析版本不存在或不属于该文档")
        return version

    def build_plan(self, document_id: str, kind: str,
                   version_id: str | None = None) -> AnalysisPlanOutcome:
        """只做本地输入规划，零外部调用。"""
        document = self.require_document(document_id)
        version = self.resolve_version(document, version_id)
        blocks = self.repository.blocks(version.id)
        system_chars = len(extraction_system_prompt() if kind == "extraction"
                           else summary_system_prompt())
        prompt_version, _protocol = KIND_VERSIONS[kind]
        plan = build_plan(kind=kind, blocks=blocks, document_name=document.filename,
                          version_id=version.id, settings=self.settings,
                          prompt_version=prompt_version, system_prompt_chars=system_chars,
                          parse_quality_status=version.quality_status,
                          parse_is_legacy=version.is_legacy)
        return AnalysisPlanOutcome(plan=plan, version_id=version.id, document=document,
                                   is_active_version=bool(
                                       document.active_parse_version_id == version.id))

    def plan_response(self, outcome: AnalysisPlanOutcome, kind: str) -> AnalysisPlan:
        """把规划结果转成契约响应；不泄漏模型原始内容与内部路径。"""
        plan = outcome.plan
        _prompt_version, protocol_version = KIND_VERSIONS[kind]
        batches = [
            AnalysisPlanBatch(batch_id=f"b{index + 1}", order_index=index,
                              unit_ids=[unit.unit_id for unit in batch],
                              unit_count=len(batch),
                              chars=sum(len(unit.text) for unit in batch),
                              message_chars=plan.batch_message_chars[index])
            for index, batch in enumerate(plan.batches)
        ]
        limitations = self._plan_limitations(plan)
        coverage = AnalysisPlanCoverage(
            total_units=len(plan.units), planned_units=len(plan.planned_units()),
            total_chars=plan.total_chars, excluded_units=plan.excluded_units,
            excluded_reasons={EXCLUDE_REASONS.get(code, code): count
                              for code, count in sorted(plan.excluded_counts.items())},
            batch_count=len(plan.batches))
        return AnalysisPlan(
            document_id=outcome.document.id, parse_version_id=outcome.version_id, kind=kind,
            prompt_version=plan.prompt_version, protocol_version=protocol_version,
            plan_fingerprint=plan.fingerprint, is_active_version=outcome.is_active_version,
            coverage=coverage,
            limits=AnalysisPlanLimits(
                max_requests=self.settings.analysis_max_requests,
                batch_max_chars=self.settings.analysis_batch_max_chars,
                reduce_max_chars=self.settings.analysis_reduce_max_chars,
                input_max_chars=self.settings.analysis_input_max_chars,
                max_document_chars=self.settings.analysis_max_document_chars,
                max_items_per_batch=self.settings.analysis_max_items_per_batch,
                max_items_total=self.settings.analysis_max_items_total),
            batches=batches, reduce_required=plan.reduce_required,
            request_upper_bound=plan.request_upper_bound, executable=plan.executable,
            limitations=limitations, blocked_reason=plan.blocked_reason)

    @staticmethod
    def _plan_limitations(plan: AnalysisPlanResult) -> list[str]:
        """规划限制说明：把稳定原因码翻译成用户可读的中文。"""
        reasons = {
            "parse_version_invalid": "当前解析版本质量状态为 invalid，不能作为分析输入；请重新解析或改用其他版本",
            "legacy_version_unsupported": "该版本是旧库迁移生成的兼容版本，只有旧逻辑页分块、没有结构化块与来源，"
                                          "不能补造来源，因此不支持分析；请重新解析该文档",
            "no_analyzable_content": "该解析版本没有可分析的正文或表格内容",
            "document_too_large": "该解析版本的可分析文本超过单任务上限，第一版不提供“只处理前面部分却称为全文”的降级",
            "unit_over_batch_budget": "存在单个输入单元超过单批字符预算，无法在不截断表头、单位或条件的前提下送入模型",
            "batch_message_over_input_budget": "按当前配置拆分后，单批消息仍超过输入字符预算；请调小批次预算或调大输入预算",
            "reduce_message_over_input_budget": "完整原文引用的汇总消息上界超过预算，不能截断依据；请调整输入与汇总预算后重新规划",
            "request_budget_exceeded": "规划出的生成调用次数超过单个分析任务预算；第一版不会自动拆成多个收费任务",
        }
        limitations = list(plan.limitations)
        if plan.blocked_reason:
            limitations.insert(0, reasons.get(plan.blocked_reason, plan.blocked_reason))
        return limitations

    # ------------------------------------------------------------------
    # 创建、查询与重试
    # ------------------------------------------------------------------
    def submit(self, document_id: str, kind: str, payload: AnalysisJobRequest | None) -> tuple[AnalysisSubmitResponse, int]:
        """创建或复用分析任务；返回 (响应, HTTP 状态码)。

        状态码语义（任务书 5.3）：
        - 202：已创建或复用进行中的任务；
        - 200：幂等已完成（复用已有成功结果，不新增任何调用）；
        - 409：幂等键冲突、计划指纹不匹配、解析版本不可用等。

        **本方法不发起任何模型调用**，只写数据库。
        """
        request = payload or AnalysisJobRequest()
        document = self.require_document(document_id)
        # 前置检查顺序：文档存在 → 有可用解析版本 → 生成模型已配置。
        # 因此“不存在”是 404、“没有可用解析版本”是 409、缺配置是 503，
        # 且三者都在任何外部调用（以及任何本地规划开销）之前完成。
        self.resolve_version(document, None)
        self.resolve_version(document, request.parse_version_id)

        # 幂等键优先于“模型是否已配置”：已完成任务的重复提交只是读取已有结果，
        # 不产生任何调用，也不应因为此刻缺密钥而报错。
        key = (request.idempotency_key or "").strip() or None
        if key:
            existing = self.repository.find_analysis_job_by_idempotency(document_id, key)
            if existing is not None:
                stored = self.repository.analysis_job_fingerprint(existing.id, key)
                if stored and existing.plan_fingerprint:
                    expected = request_fingerprint(
                        kind, request.parse_version_id or existing.parse_version_id,
                        request.plan_fingerprint or existing.plan_fingerprint, request.regenerate)
                    if stored != expected:
                        raise TaskConflict("该幂等键已用于不同的分析请求，请更换幂等键或改用原请求参数")
                status = 200 if existing.status == "succeeded" else 202
                return AnalysisSubmitResponse(
                    job=existing, document=self.require_document(document_id), reused=True,
                    regenerated=False,
                    message="已存在相同幂等键的任务，直接复用，不会重复生成"), status

        # 同版本同配置已有成功结果：默认复用（不新增计费）；明确重新生成才新建任务。
        # 该分支同样只读数据库，因此在缺密钥时也能如实返回已有结果。
        if not request.regenerate:
            signature_probe = analysis_model_signature(self.settings)
            probe_version = self.resolve_version(document, request.parse_version_id)
            prompt_version_probe, protocol_probe = KIND_VERSIONS[kind]
            reuse = self._reusable_result(document_id, kind, probe_version.id, signature_probe,
                                         prompt_version_probe, protocol_probe)
            if reuse is not None:
                job = self.repository.get_analysis_job(reuse["job_id"])
                if job is not None:
                    if request.plan_fingerprint and request.plan_fingerprint != job.plan_fingerprint:
                        raise TaskConflict("计划指纹与已有结果不匹配，请重新查看分析范围")
                    if key:
                        bound = self.repository.bind_analysis_request_key(
                            job.id, document_id, key,
                            request_fingerprint(kind, job.parse_version_id, job.plan_fingerprint))
                        job = self.repository.get_analysis_job(bound)
                    return AnalysisSubmitResponse(
                        job=job, document=self.require_document(document_id), reused=True,
                        regenerated=False,
                        message="已复用同配置的任务；查看结果不会重新生成，如需新结果请使用“重新生成”"), (200 if job.status == "succeeded" else 202)

        if not self.ready()[0]:
            raise RagError(503, "provider_not_configured", self.ready()[1])
        outcome = self.build_plan(document_id, kind, request.parse_version_id)
        plan = outcome.plan
        version = self.repository.get_parse_version(outcome.version_id)
        prompt_version, protocol_version = KIND_VERSIONS[kind]
        signature = analysis_model_signature(self.settings)
        fingerprint = request_fingerprint(kind, outcome.version_id, plan.fingerprint or "blocked", request.regenerate)

        # 计划不可执行时提前拒绝，并说明原因（不产生任务、不产生费用）。
        if not plan.executable:
            code = plan.blocked_reason or "plan_not_executable"
            status = 409 if code in {"parse_version_invalid", "legacy_version_unsupported",
                                    "no_parse_version"} else 422
            raise RagError(status, f"analysis_plan_blocked:{code}",
                           "；".join(self._plan_limitations(plan)))

        if request.plan_fingerprint and request.plan_fingerprint != plan.fingerprint:
            # 计划变化（例如预览版本切换或预算调整）不能静默扩大费用。
            raise TaskConflict("计划指纹与当前解析版本不匹配，请重新查看分析范围后再发起生成")

        job = AnalysisJob(
            id=uuid4().hex, document_id=document_id, parse_version_id=outcome.version_id,
            kind=kind, status="queued", stage="queued",
            stage_detail="已排队，等待分析 worker 领取",
            idempotency_key=key, plan_fingerprint=plan.fingerprint,
            request_upper_bound=plan.request_upper_bound,
            # 首轮计划上界与含明确重试的总预算分开保存；重试不能重置总额。
            max_requests=self.settings.analysis_max_requests,
            requests_used=0, steps_total=len(plan.batches) + (1 if plan.reduce_required else 0),
            steps_completed=0, prompt_version=prompt_version, protocol_version=protocol_version,
            model_signature=signature,
            coverage=self._planned_coverage(plan),
            limitations=self._plan_limitations(plan),
            created_at=now_iso(), updated_at=now_iso())
        created, is_new = self.repository.create_analysis_job(
            job, plan=self._plan_payload(plan, version), input_hash=self._input_hash(plan),
            request_fingerprint=fingerprint)
        if not is_new:
            # 并发创建时的兜底（相同幂等键或活动任务被先到者占用）：复用先到者。
            return AnalysisSubmitResponse(
                job=created, document=self.require_document(document_id), reused=True,
                regenerated=False,
                message="已存在相同幂等键或正在执行的任务，已复用该任务"), 202
        return AnalysisSubmitResponse(
            job=created, document=self.require_document(document_id), reused=False,
            regenerated=bool(request.regenerate),
            message=f"分析任务已创建并排队（预计最多 {plan.request_upper_bound} 次生成调用）；"
                    "请由 worker 进程执行（python -m app.analysis_worker）" +
                    ("；本次是明确重新生成，将产生新的结果版本" if request.regenerate else "")), 202

    def _planned_coverage(self, plan: AnalysisPlanResult) -> dict:
        """任务创建时固定的覆盖口径；未处理原因在发布结果时按实际情况改写。"""
        return {
            "total_units": len(plan.units),
            "processed_units": 0,
            "unresolved_units": len(plan.units),
            "unresolved_reasons": {"not_processed_yet": len(plan.units)} if plan.units else {},
            "excluded_units": plan.excluded_units,
            "excluded_reasons": dict(plan.excluded_counts),
            "batch_total": len(plan.batches),
            "batch_completed": 0,
            "reduce_completed": False,
            "complete": False,
            "resolved_original_refs": 0,
            "unresolved_original_refs": 0,
        }

    @staticmethod
    def _plan_payload(plan: AnalysisPlanResult, version) -> dict:
        """写入任务的计划快照：执行阶段据此重建单元与批次，不重新规划。"""
        return {
            "protocol_version": plan.protocol_version,
            "prompt_version": plan.prompt_version,
            "fingerprint": plan.fingerprint,
            "kind": plan.kind,
            "version_id": version.id,
            "reduce_required": plan.reduce_required,
            "request_upper_bound": plan.request_upper_bound,
            "excluded_counts": plan.excluded_counts,
            "excluded_units": plan.excluded_units,
            "total_chars": plan.total_chars,
            "limitations": plan.limitations,
            "batches": [
                {"batch_id": f"b{index + 1}", "order_index": index,
                 "unit_ids": [unit.unit_id for unit in batch],
                 "message_chars": plan.batch_message_chars[index]}
                for index, batch in enumerate(plan.batches)
            ],
        }

    @staticmethod
    def _input_hash(plan: AnalysisPlanResult) -> str:
        """输入清单哈希：单元 ID 与正文的稳定摘要，用于判断输入是否被改动。"""
        digest = sha256()
        for batch in plan.batches:
            for unit in batch:
                digest.update(unit.unit_id.encode())
                digest.update(unit.text.encode())
        return digest.hexdigest()

    def _reusable_result(self, document_id: str, kind: str, version_id: str, model_signature: str,
                         prompt_version: str, protocol_version: str) -> dict | None:
        """查找同文档、同解析版本、同 Prompt／协议／模型配置的成功结果。"""
        for summary in self.repository.list_analysis_results(document_id, kind):
            if summary.parse_version_id != version_id:
                continue
            if summary.protocol_version != protocol_version:
                continue
            if summary.prompt_version != prompt_version:
                continue
            row = self.repository.get_analysis_result_row(summary.id)
            if row is None or row["model_signature"] != model_signature:
                continue
            return row
        return None

    def get_job(self, job_id: str) -> AnalysisJob:
        job = self.repository.get_analysis_job(job_id)
        if job is None:
            raise RagError(404, "analysis_job_not_found", "分析任务不存在")
        return job

    def retry_job(self, job_id: str) -> AnalysisJob:
        """用户明确重试：预算不重置，复用已校验步骤，只做未完成部分。

        不允许对 succeeded 任务重试（要重新生成必须新建任务，产生新的结果版本）；
        不允许对 queued／running 重试。

        说明：失败与不确定调用同样占用预算，因此重试需要任务还有剩余额度；
        如果本次失败已经用掉全部额度，这里会明确拒绝而不是把预算重置为 0，
        用户应新建任务（同样是显式操作，会产生新的结果版本）。
        """
        job = self.get_job(job_id)
        if job.status in {"queued", "running"}:
            raise RagError(409, "analysis_job_active", "该任务仍在排队或执行中，无需重试")
        if job.status == "succeeded":
            raise RagError(409, "analysis_job_succeeded",
                           "该任务已成功；如需重新生成请使用“重新生成”，它会创建新的结果版本")
        if job.status == "cancelled":
            raise RagError(409, "analysis_job_cancelled",
                           "该任务已被取消；如需重新生成请新建任务")
        remaining = job.max_requests - job.requests_used
        if remaining <= 0:
            raise RagError(409, "analysis_budget_exhausted",
                           "该任务的生成预算已用尽（失败与不确定调用也计入预算）；"
                           "请新建任务而不是重置预算")
        if not self.repository.set_analysis_stage_after_retry(job_id):
            raise RagError(409, "analysis_retry_conflict", "任务状态已变化，请刷新后重试")
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> dict:
        """取消后续处理；重复取消幂等。不承诺上游已发出的调用停止计费。"""
        job = self.get_job(job_id)
        if job.status in {"succeeded", "failed", "cancelled", "needs_attention"}:
            # 终态或待确认状态重复取消：保持幂等，返回当前状态而不报错。
            return {"job_id": job_id, "status": job.status, "cancelled": False,
                    "message": "该任务已结束或需要人工确认，无需再次取消"}
        self.repository.request_analysis_cancel(job_id)
        current = self.get_job(job_id)
        return {"job_id": job_id, "status": current.status, "cancelled": current.status == "cancelled",
                "message": "已取消后续处理；已在途的生成请求可能继续产生费用，系统不承诺供应商停止计费"
                if current.status != "cancelled" else
                "任务在开始执行前已取消，未产生任何模型调用"}

    # ------------------------------------------------------------------
    # 结果读取与导出
    # ------------------------------------------------------------------
    def list_results(self, document_id: str, kind: str | None) -> list[AnalysisResultSummary]:
        self.require_document(document_id)
        return self.repository.list_analysis_results(document_id, kind)

    def job_results(self, job_id: str) -> list[AnalysisResultSummary]:
        job = self.get_job(job_id)
        return [item for item in self.repository.list_analysis_results(job.document_id, job.kind)
                if item.job_id == job_id]

    def get_result(self, result_id: str) -> AnalysisResult:
        row = self.repository.analysis_result_payload(result_id)
        if row is None:
            raise RagError(404, "analysis_result_not_found", "分析结果不存在")
        return self.to_result(row)

    def to_result(self, row: dict) -> AnalysisResult:
        """由已校验结果行与检查点正文组装响应契约。

        结果是**不可变**的：这里只做读取与结构组装，绝不重新生成、
        也绝不返回模型原始正文或未校验内容。
        """
        payload = row.get("payload") or {}
        coverage = json.loads(row["coverage_json"]) if row.get("coverage_json") else {}
        warnings = json.loads(row["warnings_json"]) if row.get("warnings_json") else []
        limitations = json.loads(row["limitations_json"]) if row.get("limitations_json") else []
        document = self.repository.get(row["document_id"])
        version = self.repository.get_parse_version(row["parse_version_id"])
        result = AnalysisResult(
            id=row["id"], job_id=row["job_id"], document_id=row["document_id"],
            parse_version_id=row["parse_version_id"], kind=row["kind"],
            is_active_version=bool(document and document.active_parse_version_id == row["parse_version_id"]),
            coverage=AnalysisCoverage(**coverage), quality_warnings=warnings,
            limitations=limitations, prompt_version=row["prompt_version"],
            protocol_version=row["protocol_version"], model_signature=row["model_signature"],
            parse_parser_name=version.parser_name if version else None,
            parse_quality_status=version.quality_status if version else None,
            requests_used=row["requests_used"], created_at=row["created_at"])
        if row["kind"] == "extraction":
            result.extraction = AnalysisExtractionResult(
                items=[AnalysisExtractionItem(**item) for item in payload.get("items", [])],
                sections=payload.get("sections", {}),
                citations=[AnalysisCitation(**item) for item in payload.get("citations", [])])
        else:
            result.summary = AnalysisSummaryResult(
                topic_overview=payload.get("topic_overview", ""),
                main_points=[AnalysisSummaryPoint(**item) for item in payload.get("main_points", [])],
                exceptions=[AnalysisSummaryPoint(**item) for item in payload.get("exceptions", [])],
                limitations=payload.get("limitations", []),
                citations=[AnalysisCitation(**item) for item in payload.get("citations", [])])
        return result

    def export(self, result_id: str, fmt: str) -> tuple[str, str, str]:
        """导出已校验结果：返回 (正文, MIME 类型, 下载文件名)。

        导出内容只来自已校验结构，因此天然不含密钥、服务器路径、模型思考或未校验
        原始响应。Markdown 中的危险语法按文本化处理，文件名做安全化。
        """
        result = self.get_result(result_id)
        document = self.repository.get(result.document_id)
        if fmt == "json":
            body = json.dumps(self._export_payload(result, document), ensure_ascii=False, indent=2)
            return body, "application/json", self._export_filename(result, document, "json")
        return self._export_markdown(result, document), "text/markdown; charset=utf-8", \
            self._export_filename(result, document, "md")

    @staticmethod
    def _export_filename(result: AnalysisResult, document: Document | None, suffix: str) -> str:
        """下载文件名安全化：只保留基本名，去掉路径分隔符与控制字符。"""
        import re

        stem = (document.filename if document else result.document_id) or "document"
        stem = stem.replace("\\", "/").split("/")[-1]
        stem = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", stem).strip("._") or "document"
        kind = "摘要" if result.kind == "summary" else "提取"
        return f"{stem}-{kind}-{result.id[:8]}.{suffix}"

    def _export_payload(self, result: AnalysisResult, document: Document | None) -> dict:
        """JSON 导出结构：类型、解析版本、生成时间、引用与限制都在其中。"""
        payload = {
            "result_id": result.id,
            "job_id": result.job_id,
            "kind": result.kind,
            "document": {"id": result.document_id,
                         "filename": document.filename if document else None},
            "parse_version_id": result.parse_version_id,
            "is_active_version": result.is_active_version,
            "created_at": result.created_at,
            "prompt_version": result.prompt_version,
            "protocol_version": result.protocol_version,
            "requests_used": result.requests_used,
            "coverage": result.coverage.model_dump(),
            "limitations": result.limitations,
            "quality_warnings": [warning.model_dump() if hasattr(warning, "model_dump") else warning
                                 for warning in result.quality_warnings],
        }
        if result.extraction is not None:
            payload["extraction"] = {
                "items": [item.model_dump() for item in result.extraction.items],
                "sections": result.extraction.sections,
                "citations": [citation.model_dump() for citation in result.extraction.citations],
            }
        if result.summary is not None:
            payload["summary"] = {
                "topic_overview": result.summary.topic_overview,
                "main_points": [point.model_dump() for point in result.summary.main_points],
                "exceptions": [point.model_dump() for point in result.summary.exceptions],
                "limitations": result.summary.limitations,
                "citations": [citation.model_dump() for citation in result.summary.citations],
            }
        return payload

    def _export_markdown(self, result: AnalysisResult, document: Document | None) -> str:
        """Markdown 导出：逐条事实后跟后端生成的引用标记，并附引用清单与限制。"""
        def marker(refs: list[int]) -> str:
            return "".join(f"[{ref}]" for ref in sorted(refs))

        def esc(text: str) -> str:
            # 危险语法文本化：避免导出文件被当成含 HTML/脚本或外部资源的内容打开。
            value = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            for char in "\\`*_{}[]()#+-!|":
                value = value.replace(char, "\\" + char)
            # 使用实体保留可见换行，不让原文换行创建新的 Markdown 结构。
            return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "&#10;")

        lines: list[str] = []
        title = "文档摘要" if result.kind == "summary" else "数据 / 结论 / 观点提取"
        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"- 文档：{esc(document.filename) if document else result.document_id}")
        lines.append(f"- 解析版本：{result.parse_version_id}"
                     + ("（当前预览版本）" if result.is_active_version else "（历史解析版本）"))
        lines.append(f"- 结果 ID：{result.id}")
        lines.append(f"- 生成时间：{result.created_at}")
        lines.append(f"- Prompt 版本：{result.prompt_version} · 协议版本：{result.protocol_version}")
        lines.append(f"- 本次生成调用：{result.requests_used} 次")
        coverage = result.coverage
        lines.append(f"- 覆盖：处理 {coverage.processed_units} / 总输入单元 {coverage.total_units}；"
                     f"完成批次 {coverage.batch_completed} / {coverage.batch_total}"
                     + ("；已执行汇总" if coverage.reduce_completed else "")
                     + ("；覆盖完整" if coverage.complete else "；覆盖不完整"))
        if coverage.unresolved_units:
            reasons = "；".join(f"{key}×{value}" for key, value in sorted(coverage.unresolved_reasons.items()))
            lines.append(f"- 未处理单元：{coverage.unresolved_units}（{reasons or '原因未记录'}）")
        if coverage.excluded_units:
            reasons = "；".join(f"{key}×{value}" for key, value in sorted(coverage.excluded_reasons.items()))
            lines.append(f"- 排除的非正文单元：{coverage.excluded_units}（{reasons or '未记录'}）")
        lines.append("")

        if result.summary is not None:
            lines.append("## 主题概述")
            lines.append(esc(result.summary.topic_overview) or "（无）")
            lines.append("")
            lines.append("## 主要内容与结论")
            for point in result.summary.main_points:
                lines.append(f"- {esc(point.text)}{marker(point.refs)}")
            if result.summary.exceptions:
                lines.append("")
                lines.append("## 例外、冲突与限制")
                for point in result.summary.exceptions:
                    lines.append(f"- {esc(point.text)}{marker(point.refs)}")
            citations = result.summary.citations
        else:
            labels = {"data": "数据", "conclusion": "结论", "viewpoint": "观点"}
            items = result.extraction.items if result.extraction else []
            sections = result.extraction.sections if result.extraction else {}
            for kind in ("data", "conclusion", "viewpoint"):
                lines.append(f"## {labels[kind]}")
                group = [item for item in items if item.kind == kind]
                if not group:
                    lines.append("（本次输入中没有该类可提取内容）"
                                 if sections.get(kind) == "none" else "（未处理）")
                    lines.append("")
                    continue
                for item in group:
                    detail = []
                    if item.name:
                        detail.append(f"对象：{esc(item.name)}")
                    if item.value_text:
                        detail.append(f"数值：{esc(item.value_text)}")
                    if item.unit:
                        detail.append(f"单位：{esc(item.unit)}")
                    if item.period:
                        detail.append(f"时间：{esc(item.period)}")
                    if item.subject:
                        detail.append(f"主体：{esc(item.subject)}")
                    if item.scope:
                        detail.append(f"口径/条件：{esc(item.scope)}")
                    suffix = f"（{'；'.join(detail)}）" if detail else ""
                    lines.append(f"- {esc(item.content)}{marker(item.refs)}{suffix}")
                lines.append("")
            citations = result.extraction.citations if result.extraction else []

        if citations:
            lines.append("## 引用与来源")
            for citation in citations:
                lines.append(f"- [{citation.reference_id}] {esc(citation.quote)}")
                location = " / ".join(source.label() for source in citation.sources) or "来源定位信息有限"
                lines.append(f"  - 位置：{esc(location)}")
        if result.limitations:
            lines.append("")
            lines.append("## 适用边界")
            for text in result.limitations:
                lines.append(f"- {esc(text)}")
        lines.append("")
        lines.append("> 说明：引用为解析版本原文的连续片段（仅统一 CRLF），逐字保留空格与标点。"
                     "本文件由已校验结果生成，不含模型思考或未校验原始输出。")
        return "\n".join(lines)


__all__ = [
    "KIND_VERSIONS",
    "AnalysisJobService",
    "AnalysisPlanOutcome",
    "analysis_model_signature",
    "request_fingerprint",
]
