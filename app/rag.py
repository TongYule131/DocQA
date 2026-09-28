"""RAG 问答服务：前置检查、检索、证据固定、生成与校验编排（工程包 A/B/C/D）。

调用顺序（对应任务书第 3 节）：

    RagService.answer
      → 前置检查（请求 → 文档 → 解析版本 → 索引兼容 → 在线模型已配置）
      → DocumentIndex.search（一次查询 embedding + 一次检索）
      → EvidenceBuilder（固定版本、去重、预算、服务器分配引用编号）
      → PromptBuilder（固定系统规则 + 不可信问题/参考资料）
      → 一次非流式生成
      → 严格结构与引用校验
      → 由已校验结构渲染 Markdown 与引用列表

硬性约束：

- 一次提问最多一次查询 embedding、一次生成调用；任何前置条件失败都不调用生成；
- 不自动重试模型、不自动改写答案、不退化为脱离资料的普通聊天；
- 回答期间版本或索引变化仍使用固定快照，不二次生成、不二次计费；
- 原解析与索引不因问答成败被修改（本模块只读 repository 与 index）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from uuid import uuid4

from app import vector_index
from app.config import Settings
from app.rag_context import (
    EvidencePack,
    RagError,
    build_evidence,
)
from app.rag_prompts import (
    INSUFFICIENT_EVIDENCE_TEXT,
    PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
)
from app.rag_validation import (
    ModelOutputError,
    ValidatedOutput,
    render_markdown,
    validate_model_output,
)
from app.repository import Repository
from app.schemas import (
    QualityWarning,
    RagAnswer,
    RagCitation,
    RagFact,
    RagRetrievalStats,
    RagTimings,
)

logger = logging.getLogger(__name__)

# 与本次证据相关的质量告警优先展示的稳定告警码（公式缓存、OCR、结构限制等）。
_HIGH_PRIORITY_WARNING_CODES = (
    "formula", "ocr", "equation", "chunking", "table", "source", "empty",
)


@dataclass
class RagOutcome:
    """服务层返回结果：响应契约 + 本次诊断信息（不进入 HTTP 响应）。"""
    answer: RagAnswer
    diagnostics: dict = field(default_factory=dict)


class RagService:
    """单文档、单轮、非流式 RAG 问答编排器。

    repository / index / model 可注入：测试可替换为 mock 传输或可控桩，
    但编排、上下文构建、输出解析、校验与渲染始终是生产代码路径。
    """

    def __init__(self, settings: Settings, repository: Repository,
                 index: vector_index.DocumentIndex, model):
        self.settings = settings
        self.repository = repository
        self.index = index
        self.model = model

    # ------------------------------------------------------------------
    # 前置检查
    # ------------------------------------------------------------------
    def ready(self) -> tuple[bool, str]:
        """问答能力是否可用；配置状态只代表配置存在，不代表账户与服务可用。"""
        if not self.settings.embedding_api_key.strip():
            return False, "检索问答需要先配置 EMBEDDING_API_KEY 并重启服务"
        if not self.settings.deepseek_api_key.strip():
            return False, "回答生成需要先配置 DEEPSEEK_API_KEY 并重启服务"
        return True, ""

    def answer(self, document_id: str, question: str) -> RagOutcome:
        started = time.perf_counter()
        # 前置检查顺序：文档 → 解析版本 → 索引兼容 → 在线模型配置。
        # 无效请求与不存在的文档在路由层就被拒绝，不会走到这里调用查询 embedding。
        document = self.repository.get(document_id)
        if document is None:
            raise RagError(404, "document_not_found", "文档不存在")

        start_version_id = document.active_parse_version_id
        if not start_version_id:
            raise RagError(409, "no_parse_version", "该文档尚无成功解析版本，请先完成解析")

        version = self.repository.get_parse_version(start_version_id)
        if version is None:
            raise RagError(409, "no_parse_version", "该文档的解析版本不可读，请重新解析")
        if version.quality_status == "invalid":
            raise RagError(409, "parse_version_invalid",
                           "当前解析版本质量状态为 invalid，不能作为问答证据；请重新解析或使用其他版本")

        info = self.index.index_info(document_id)
        if info["status"] != "indexed":
            if info["status"] == "stale":
                reason = info["error"] or "索引已过期"
                raise RagError(409, "index_not_compatible", f"{reason}，请先建立与当前配置兼容的有效索引")
            raise RagError(409, "index_missing", "该文档还没有可用索引，请先建立索引后再提问")
        active = self.repository.active_index(document_id)
        if active is None or active.model_signature != self.index.embedding.signature:
            raise RagError(409, "index_not_compatible",
                           "当前可用索引与本次 Embedding 配置不兼容，请重新建立索引")

        # 两个在线模型都必须已配置：缺配置时返回 503，并且连查询 embedding 也不调用。
        ok, reason = self.ready()
        if not ok:
            raise RagError(503, "provider_not_configured", reason)

        # 固定规则和问题本身已超限时直接报错，不能伪装成“没有证据”。
        system_prompt = build_system_prompt()
        context_cap = self._context_cap(system_prompt, question, document.filename)
        # 唯一一次查询 embedding + 检索；检索固定版本由 DocumentIndex 返回。
        # 先记住检索前的活动索引，用于识别“检索期间索引被切换”的情况。
        index_before_search = info["index_id"]
        retrieval_started = time.perf_counter()
        search_result = self.index.search(document_id, question,
                                          self.settings.rag_retrieval_k)
        retrieval_ms = _elapsed_ms(retrieval_started)

        snapshot = _IndexSnapshot(
            document_id=document_id,
            filename=document.filename,
            index_id=search_result.get("index_id"),
            parse_version_id=search_result.get("parse_version_id"),
            is_legacy=bool(search_result.get("is_legacy")),
            start_version_id=start_version_id,
            pre_search_index_id=index_before_search,
        )
        evidence_version = self._verify_snapshot(snapshot)

        # 证据预算：先反推“参考资料序列化后还能占多少字符”，再整块装入。
        build = build_evidence(search_result, self.settings,
                               version_warnings=evidence_version.warnings, context_cap=context_cap,
                               expected_document_id=document_id)
        if build.fatal_reason:
            # 证据混入其他文档或版本：受控失败，不生成、不拼接。
            raise RagError(409, "evidence_scope_mismatch", build.fatal_reason)
        pack = build.pack
        warnings = list(build.warnings)
        limitations = list(build.limitations)
        warnings.extend(self._snapshot_warnings(snapshot))

        if not pack.items:
            # 没有合格证据时直接返回依据不足，不调用 DeepSeek。
            warnings.extend(self._drop_warnings(pack.drop_reasons))
            limitations.append("本次请求没有可送入模型的证据块，因此没有调用生成模型")
            return self._insufficient(
                snapshot, pack, warnings, limitations,
                retrieval_ms=retrieval_ms, started=started, generated=False)

        if not self.settings.deepseek_api_key.strip():
            raise RagError(503, "provider_not_configured",
                           "尚未配置 DEEPSEEK_API_KEY，请填写本地 .env 后重启服务")

        user_prompt = build_user_prompt(question=question, document_name=document.filename,
                                        evidence_payload=pack.payload())
        if len(system_prompt) + len(user_prompt) > self.settings.rag_input_max_chars:
            raise RagError(422, "rag_input_too_large",
                           "系统规则、问题与固定元数据已超过输入字符预算；"
                           "请缩短问题或调大 DOCQA_RAG_INPUT_MAX_CHARS 后重试")

        # 一次非流式生成；不重试、不改写、不追加第二次调用。
        generation_started = time.perf_counter()
        raw = self.model.generate(system_prompt, user_prompt)
        generation_ms = _elapsed_ms(generation_started)

        try:
            validated = validate_model_output(raw, pack)
        except ModelOutputError as exc:
            # 只记录非敏感原因码：不带原文、完整输出与凭证。
            logger.warning("RAG 输出校验失败：reason=%s", exc.reason)
            raise RagError(502, "rag_output_invalid",
                           "模型输出未通过结构或引用校验，请重试；本次不会返回未校验内容") from None

        warnings.extend(self._quote_warnings(pack, validated))
        warnings.extend(self._post_generation_warnings(snapshot))
        answer = self._build_answer(
            snapshot, pack, validated, question=question, warnings=warnings,
            limitations=limitations, retrieval_ms=retrieval_ms, generation_ms=generation_ms,
            started=started, generated=True)
        return RagOutcome(answer=answer, diagnostics={
            "selected": [{"reference_id": item.reference_id, "chunk_id": item.chunk_id,
                          "page": item.page, "text": item.text,
                          "document_id": item.document_id} for item in pack.items],
            "dropped_candidates": len(pack.drop_reasons),
            "context_chars": pack.context_chars,
            "input_chars": len(system_prompt) + len(user_prompt),
        })

    # ------------------------------------------------------------------
    # 快照与预算
    # ------------------------------------------------------------------
    def _verify_snapshot(self, snapshot: "_IndexSnapshot"):
        """校验检索结果确实属于本次固定的索引与解析版本。"""
        if snapshot.pre_search_index_id and snapshot.index_id and \
                snapshot.pre_search_index_id != snapshot.index_id:
            # 检索期间活动索引被切换：本次证据虽然仍完整，但已不是提问开始时看到的索引。
            raise RagError(409, "index_changed_midflight",
                           "检索期间该文档的可用索引发生变化，请刷新后重试提问")
        version_id = snapshot.parse_version_id
        if not version_id:
            raise RagError(409, "index_not_compatible", "检索结果缺少解析版本信息，请重新建立索引")
        version = self.repository.get_parse_version(version_id)
        if version is None or version.document_id != snapshot.document_id:
            raise RagError(409, "evidence_scope_mismatch",
                           "检索结果的解析版本不属于该文档，已拒绝本次回答")
        if version.quality_status == "invalid":
            raise RagError(409, "parse_version_invalid", "索引绑定的解析版本质量无效，不能用于问答")
        return version

    def _context_cap(self, system_prompt: str, question: str, filename: str | None) -> int:
        """反推参考资料可用的字符预算，保证最终消息体不超限。

        预算里显式计入问题、文件名、转义字符、固定包装与输出 schema 的字符数；
        字符预算不等于 token 预算，这里只做本地输入限流。
        """
        empty_user = build_user_prompt(question=question, document_name=filename,
                                       evidence_payload=[])
        overhead = len(system_prompt) + len(empty_user)
        if overhead > self.settings.rag_input_max_chars:
            raise RagError(422, "rag_input_too_large", "系统规则、问题与元数据超过输入字符预算，请缩短问题或调整配置")
        # empty_user 已包含 [] 两个字符，实际证据列表替换该字段而不是追加。
        available = self.settings.rag_input_max_chars - overhead + 2
        return max(min(self.settings.rag_context_max_chars, available), 0)

    def _snapshot_warnings(self, snapshot: "_IndexSnapshot") -> list[QualityWarning]:
        """固定版本信息与检索时版本不一致时的告警（固定快照仍然有效）。"""
        warnings: list[QualityWarning] = []
        if snapshot.start_version_id and snapshot.parse_version_id and \
                snapshot.start_version_id != snapshot.parse_version_id:
            warnings.append(_warning(
                "parse_version_changed_before_answer", "warning",
                "当前预览与本次索引绑定的版本不同；回答仅依据索引绑定的历史解析版本"))
        return warnings

    def _post_generation_warnings(self, snapshot: "_IndexSnapshot") -> list[QualityWarning]:
        """生成结束后再次对比指针：只提示，不改变已固定的证据、不二次生成。"""
        warnings: list[QualityWarning] = []
        document = self.repository.get(snapshot.document_id)
        if document is not None and document.active_parse_version_id and \
                document.active_parse_version_id != snapshot.start_version_id:
            warnings.append(_warning(
                "parse_version_changed_during_generation", "warning",
                "生成本次回答期间该文档发布了新的解析版本；本次答案仍全部来自固定的历史版本索引"))
        info = self.index.index_info(snapshot.document_id)
        if info["index_id"] and snapshot.index_id and info["index_id"] != snapshot.index_id:
            warnings.append(_warning(
                "index_changed_during_generation", "warning",
                "生成本次回答期间该文档的活动索引已切换；本次答案仍来自提问时固定的索引"))
        return warnings

    def _drop_warnings(self, drop_reasons: list[str]) -> list[QualityWarning]:
        """预算或去重舍弃证据的说明：数量与原因码不含原文。"""
        if not drop_reasons:
            return []
        return [_warning("evidence_dropped", "info",
                         f"本次有 {len(drop_reasons)} 条候选片段未获得引用编号"
                         "（超出送入模型的块数上限、超出字符预算或与已选片段重复）")]

    def _quote_warnings(self, pack: EvidencePack, validated: ValidatedOutput) -> list[QualityWarning]:
        """已校验但未被使用的引述：属于低优先级提示，不改变校验结论。"""
        used = validated.used_refs()
        unused = sorted(set(validated.quotes.keys()) - used)
        if not unused:
            return []
        return [_warning("unused_quote_ignored", "info",
                         f"模型为 {len(unused)} 个未使用编号提供了引述，已在引用列表中忽略")]

    # ------------------------------------------------------------------
    # 组装响应
    # ------------------------------------------------------------------
    def _citations(self, pack: EvidencePack, quotes: dict[int, str]) -> list[RagCitation]:
        """引用列表：只包含正文实际使用的引用，按 reference_id 排序。

        文档 ID、chunk ID、页码与来源全部从本次证据映射复制，模型无法提供也无法伪造。
        """
        citations: list[RagCitation] = []
        for ref in sorted(quotes.keys()):
            item = pack.item_by_ref(ref)
            if item is None:
                continue
            citations.append(RagCitation(
                reference_id=ref, document_id=item.document_id, chunk_id=item.chunk_id,
                parse_version_id=item.parse_version_id, page=item.page,
                quote=quotes[ref], sources=list(item.sources)))
        return citations

    def _build_answer(self, snapshot: "_IndexSnapshot", pack: EvidencePack,
                      validated: ValidatedOutput, *, question: str,
                      warnings: list[QualityWarning], limitations: list[str],
                      retrieval_ms: int, generation_ms: int, started: float,
                      generated: bool) -> RagAnswer:
        """由已校验结构生成最终响应；is_old_version / is_current_index 以响应前的指针为准。"""
        document = self.repository.get(snapshot.document_id)
        info = self.index.index_info(snapshot.document_id)
        is_old_version = bool(document and document.active_parse_version_id
                              and document.active_parse_version_id != snapshot.parse_version_id)
        is_current_index = bool(info["index_id"] and snapshot.index_id
                                and info["index_id"] == snapshot.index_id)
        if is_old_version:
            limitations.append("本次回答依据历史解析版本；当前预览已更新，如需最新内容请重建索引后重新提问")
        if not is_current_index and info["index_id"]:
            limitations.append("本次回答依据提问时固定的索引；该文档的活动索引此后已切换")

        conclusion = [RagFact(text=fact.text, refs=sorted(fact.refs)) for fact in validated.conclusion]
        explanation = [RagFact(text=fact.text, refs=sorted(fact.refs)) for fact in validated.explanation]
        if validated.status == "insufficient_evidence":
            # 该状态由后端提供固定兜底文本，引用与澄清问题为空。
            answer_text = INSUFFICIENT_EVIDENCE_TEXT
            citations: list[RagCitation] = []
            question_list: list[str] = []
            conclusion, explanation = [], []
        else:
            answer_text = render_markdown(validated)
            citations = self._citations(pack, validated.quotes)
            question_list = list(validated.clarification_questions)

        return RagAnswer(
            answer_id=uuid4().hex,
            status=validated.status,
            answer=answer_text,
            clarification_questions=question_list,
            citations=citations,
            conclusion=conclusion,
            explanation=explanation,
            document_id=snapshot.document_id,
            index_id=snapshot.index_id,
            parse_version_id=snapshot.parse_version_id,
            is_old_version=is_old_version,
            is_current_index=is_current_index,
            prompt_version=PROMPT_VERSION,
            retrieval=RagRetrievalStats(
                candidate_count=len(pack.candidates),
                selected_count=len(pack.items),
                context_chars=pack.context_chars,
                truncated=pack.truncated),
            quality_warnings=_presentable_warnings(warnings),
            limitations=_dedupe(limitations),
            timings_ms=RagTimings(retrieval=retrieval_ms, generation=generation_ms,
                                  total=_elapsed_ms(started)),
        )

    def _insufficient(self, snapshot: "_IndexSnapshot", pack: EvidencePack,
                      warnings: list[QualityWarning], limitations: list[str], *,
                      retrieval_ms: int, started: float, generated: bool) -> RagOutcome:
        """合法依据不足：200 + insufficient_evidence，固定兜底文本，引用为空。"""
        document = self.repository.get(snapshot.document_id)
        info = self.index.index_info(snapshot.document_id)
        is_old_version = bool(document and document.active_parse_version_id
                              and document.active_parse_version_id != snapshot.parse_version_id)
        is_current_index = bool(info["index_id"] and snapshot.index_id
                                and info["index_id"] == snapshot.index_id)
        if is_old_version:
            limitations.append("本次检索依据历史解析版本；当前预览已更新")
        answer = RagAnswer(
            answer_id=uuid4().hex,
            status="insufficient_evidence",
            answer=INSUFFICIENT_EVIDENCE_TEXT,
            clarification_questions=[],
            citations=[],
            conclusion=[],
            explanation=[],
            document_id=snapshot.document_id,
            index_id=snapshot.index_id,
            parse_version_id=snapshot.parse_version_id,
            is_old_version=is_old_version,
            is_current_index=is_current_index,
            prompt_version=PROMPT_VERSION,
            retrieval=RagRetrievalStats(
                candidate_count=len(pack.candidates),
                selected_count=len(pack.items),
                context_chars=pack.context_chars,
                truncated=pack.truncated),
            quality_warnings=_presentable_warnings(warnings),
            limitations=_dedupe(limitations),
            timings_ms=RagTimings(retrieval=retrieval_ms, generation=0,
                                  total=_elapsed_ms(started)),
        )
        return RagOutcome(answer=answer, diagnostics={
            "insufficient": True, "generated": generated,
            "candidate_count": len(pack.candidates),
        })


@dataclass
class _IndexSnapshot:
    """本次请求固定的检索快照；回答期间发生的版本与索引变化不影响它。"""
    document_id: str
    filename: str | None
    index_id: str | None
    parse_version_id: str | None
    is_legacy: bool
    start_version_id: str | None
    # 检索前观察到的活动索引，用于识别检索期间的索引切换。
    pre_search_index_id: str | None = None


def _elapsed_ms(started: float) -> int:
    """真实非负耗时（毫秒），不编造 token 用量或正确率。"""
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def _warning(code: str, severity: str, message: str) -> QualityWarning:
    """构造面向页面的质量告警；不包含原文与内部标识。"""
    return QualityWarning(code=code, message=message, severity=severity, scope="document")


def _presentable_warnings(warnings: list[QualityWarning]) -> list[QualityWarning]:
    """按告警码去重并排序：公式缓存、OCR 等与证据相关的限制优先展示。"""
    deduped: dict[str, QualityWarning] = {}
    for warning in warnings:
        deduped.setdefault(warning.code, warning)
    def priority(warning: QualityWarning) -> tuple[int, str]:
        related = 0 if any(key in warning.code for key in _HIGH_PRIORITY_WARNING_CODES) else 1
        severity = {"error": 0, "warning": 1, "info": 2}.get(warning.severity, 3)
        return (related * 10 + severity, warning.code)
    return sorted(deduped.values(), key=priority)


def _dedupe(values: list[str]) -> list[str]:
    """保持顺序去重，避免同一限制在多条路径上重复出现。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = ["RagError", "RagOutcome", "RagService"]
