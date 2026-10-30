"""RAG 问答服务：前置检查、检索、证据固定、生成与校验编排（工程包 A/B/C/D）。

调用顺序（对应任务书第 3 节）：

    RagService.answer
      → 前置检查（请求 → 文档 → 解析版本 → 索引兼容 → 在线模型已配置）
      → DocumentIndex.search（一次查询 embedding + 一次检索）
      → EvidenceBuilder（固定版本、去重、预算、服务器分配引用编号）
      → PromptBuilder（固定系统规则 + 不可信问题/参考资料）
      → 复杂任务先规划证据与段落，再生成（旧同步请求保持一次非流式）
      → 严格结构与引用校验
      → 由已校验结构渲染 Markdown 与引用列表

硬性约束：

- 一次提问最多一次查询 embedding；复杂任务显式预留规划＋写作，其他任务一次生成；
- 不自动重试模型、不自动改写答案、不退化为脱离资料的普通聊天；
- 回答期间版本或索引变化仍使用固定快照，不二次生成、不二次计费；
- 原解析与索引不因问答成败被修改（本模块只读 repository 与 index）。
"""
from __future__ import annotations

import logging
import time
import hashlib
import json
from types import SimpleNamespace
from dataclasses import dataclass, field
from uuid import uuid4

from app import vector_index
from app.retrieval_channels import configured_reranker
from app.conversation_memory import MEMORY_RULE, MEMORY_SIGNATURE, retrieval_question, valid_memory, with_memory_prompt
from app.config import Settings
from app.rag_context import (
    EvidencePack,
    RagError,
    build_evidence,
    TASK_EVIDENCE_ORDER_VERSION,
)
from app.rag_prompts import (
    INSUFFICIENT_EVIDENCE_TEXT,
    PROMPT_VERSION,
    build_system_prompt,
    build_task_system_prompt,
    TASK_PROMPT_VERSION,
    build_user_prompt,
    build_task_user_prompt,
)
from app.quote_spans import task_segment_payload as task_evidence_payload
from app.rag_table_projection import TABLE_PROJECTION_VERSION, table_structure_signature, verify_table_snapshot
from app.rag_qualifier_cues import QUALIFIER_CUE_VERSION
from app.rag_answer_contract import ANSWER_CONTRACT_VERSION, validate_task_output
from app.rag_validation import (
    ModelOutputError,
    ValidatedOutput,
    render_markdown,
    validate_model_output,
    validate_user_condition_boundary,
)
from app.repository import Repository
from app.schemas import (
    QualityWarning,
    RagAnswer,
    RagAnswerPart,
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
    """固定文档证据的 RAG 编排器；会话由上层管理，任务支持已校验事实流。

    repository / index / model 可注入：测试可替换为 mock 传输或可控桩，
    但编排、上下文构建、输出解析、校验与渲染始终是生产代码路径。
    """

    def __init__(self, settings: Settings, repository: Repository,
                 index: vector_index.DocumentIndex, model):
        self.settings = settings
        self.repository = repository
        if repository is not None:
            repository.settings = settings
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

    def _preflight(self, document_id: str):
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
        return document, start_version_id, info

    def _configuration_signature(self) -> str:
        from app.context_budget import policy_hash
        """冻结实际影响问答的非凭证配置；排队后改配置不能静默扩大输入或费用。"""
        from app.answer_semantic_review import policy as review_policy
        names = ("deepseek_base_url", "deepseek_model", "deepseek_thinking",
                 "deepseek_reasoning_effort", "deepseek_max_tokens", "rag_retrieval_k",
                 "rag_context_k", "rag_context_max_chars", "rag_input_max_chars", "rag_min_score")
        payload = {name: getattr(self.settings, name) for name in names}
        payload['context_budget_policy']=policy_hash(self.settings)
        from app.query_understanding import policy
        payload["understanding_policy"] = policy(self.settings)
        payload["prompt_version"] = PROMPT_VERSION
        payload["question_retrieval_strategy"] = self.settings.question_retrieval_strategy
        payload["question_context_k"] = self.settings.question_context_k
        for name in ("question_context_max_chars", "question_input_max_chars", "question_candidate_k"):
            payload[name] = getattr(self.settings, name)
        payload["task_prompt_version"] = TASK_PROMPT_VERSION
        payload["task_prompt_hash"] = hashlib.sha256(build_task_system_prompt().encode()).hexdigest()
        payload["answer_contract_version"] = ANSWER_CONTRACT_VERSION
        from app.rag_answer_strategy import policy_signature
        payload["answer_strategy_signature"] = policy_signature()
        from app.rag_composition import COMPOSITION_VERSION
        payload["composition_version"] = COMPOSITION_VERSION
        payload['semantic_review']=review_policy(self.settings)
        from app.rag_extract_context import SELECTION_VERSION
        from app.rag_support_review import REVIEW_VERSION
        payload['evidence_selection_version']=SELECTION_VERSION
        from app.rag_math_evidence import MATH_EVIDENCE_VERSION
        from app.rag_slot_ranges import SLOT_RANGE_VERSION
        from app.rag_numeric_roles import NUMERIC_ROLE_VERSION
        from app.pdf_page_labels import PAGE_LABEL_VERSION
        payload.update(math_evidence_version=MATH_EVIDENCE_VERSION,slot_range_version=SLOT_RANGE_VERSION,numeric_role_version=NUMERIC_ROLE_VERSION)
        payload['page_label_version']=PAGE_LABEL_VERSION
        payload['claim_review_version']=REVIEW_VERSION
        payload["table_projection_version"] = TABLE_PROJECTION_VERSION
        payload["qualifier_cue_version"] = QUALIFIER_CUE_VERSION
        payload["task_evidence_order_version"] = TASK_EVIDENCE_ORDER_VERSION
        from app.rag_context import EVIDENCE_PACKING_VERSION
        payload['evidence_packing_version']=EVIDENCE_PACKING_VERSION
        from app.retrieval_planning import RETRIEVAL_STRATEGY_VERSION
        payload["retrieval_strategy_version"] = RETRIEVAL_STRATEGY_VERSION
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def capture_snapshot(self, document_id: str, question: str, *, published: dict | None = None, conversation_context=None) -> dict:
        """零外部调用地固定问答范围，供持久化任务在稍后读取同一个索引。"""
        if published is None:
            document, start_version_id, info = self._preflight(document_id)
        else:
            # 发布快照来自服务端权限层，不接受前端指定任意版本。草稿更新不能让客户读到新稿。
            document = self.repository.get(document_id)
            version = self.repository.get_parse_version(published.get('parse_version_id'))
            if (not document or published.get('document_id') != document_id or not version
                    or version.document_id != document_id or version.quality_status == 'invalid'
                    or version.origin_hash != published.get('origin_hash')):
                raise RagError(409, 'published_version_unavailable', '知识库发布版本不可用，请联系管理员')
            ok, reason = self.ready()
            if not ok:
                raise RagError(503, 'provider_not_configured', reason)
            start_version_id = version.id
            info = {'parse_version_id': version.id, 'index_id': published['index_id'], 'is_legacy': version.is_legacy}
        if conversation_context is not None and not valid_memory(conversation_context):
            raise RagError(409, 'conversation_context_invalid', '会话上下文无效，请重新提问')
        self._context_cap(build_task_system_prompt(include_table_structure=False, question=question) + (MEMORY_RULE if conversation_context else ''),
                          question, document.filename, memory=conversation_context)
        with self.repository.connect() as db:
            state = db.execute(
                "SELECT * FROM embedding_indexes WHERE document_id=? AND id=? AND status='indexed'",
                (document_id, info["index_id"])).fetchone()
        if state is None:
            raise RagError(409, "index_changed_midflight", "索引状态已变化，请重新提交")
        if published and state['source_signature'] != published['source_signature']:
            raise RagError(409, 'published_version_changed', '知识库发布索引的原文签名已变更，已停止处理')
        from app.rag_extract_context import SELECTION_VERSION,budget_profile
        from app.rag_support_review import REVIEW_VERSION
        from app.answer_stream import STREAM_VERSION
        from app.rag_answer_strategy import strategy_for_question
        from app.rag_composition import needs_plan
        from app.query_understanding import policy
        from app.answer_semantic_review import policy as review_policy,enabled as review_enabled
        frozen = {
            'reranker_input_hash':policy(self.settings)['reranker_replay_hash'],
            "answer_strategy":strategy_for_question(question),
            "composition_version":"evidence-composition-v1",
            "provider_requests_expected":2+int(needs_plan(question))+int(review_enabled(self.settings)),
            'semantic_review':review_policy(self.settings),
            'stream_protocol':STREAM_VERSION,
            'evidence_selection_version':SELECTION_VERSION,
            'claim_review_version':REVIEW_VERSION,
            'evidence_profile':budget_profile(question,max_items=self.settings.question_context_k,
                context_chars=self.settings.question_context_max_chars),
            "answer_contract_version": ANSWER_CONTRACT_VERSION,
            "task_prompt_version": TASK_PROMPT_VERSION,
            "document_id": document_id, "filename": document.filename,
            "start_version_id": start_version_id, "parse_version_id": info["parse_version_id"],
            "index_id": info["index_id"], "is_legacy": info["is_legacy"],
            "source_signature": state["source_signature"],
            "source_signature_algo": state["source_signature_algo"],
            "provider_signature": state["provider_signature"],
            "model_signature": self.index.embedding.signature,
            "configuration_signature": self._configuration_signature(),
            "table_projection_version": TABLE_PROJECTION_VERSION,
            "qualifier_cue_version": QUALIFIER_CUE_VERSION,
            "table_structure_signature": table_structure_signature(self.repository.blocks(info["parse_version_id"])),
            "evidence_signature": vector_index.evidence_signature(
                self.repository.chunks(document_id, info["parse_version_id"])),
        }
        if published:
            self.index.verify_snapshot(document_id, frozen)
        return frozen

    def answer(self, document_id: str, question: str, *, snapshot: dict | None = None,
               observer=None, retrieval_checkpoint: dict | None = None, composition_checkpoint: dict | None = None,
               draft_checkpoint=None,review_checkpoint=None) -> RagOutcome:
        started = time.perf_counter()
        # observer 的异常必须向上传递：失去租约、取消或预算不足时不能继续收费。
        emit = observer or (lambda _stage, _details: None)
        frozen = snapshot
        from app.answer_semantic_review import enabled as review_enabled
        if frozen is None and review_enabled(self.settings):
            raise RagError(409,'semantic_review_requires_task','启用语义复核时请使用持久化问答任务，旧同步入口不发起额外请求')
        review_on=bool(frozen and frozen.get('semantic_review',{}).get('enabled'))
        if frozen is None:
            document, start_version_id, info = self._preflight(document_id)
        else:
            from app.answer_stream import STREAM_VERSION
            if frozen.get('stream_protocol') not in (None,STREAM_VERSION):
                raise RagError(409,'question_snapshot_changed','流式协议已变更，请创建新任务')
            from app.rag_extract_context import SELECTION_VERSION,budget_profile
            from app.rag_support_review import REVIEW_VERSION
            if frozen.get('task_prompt_version')==TASK_PROMPT_VERSION and (
                    frozen.get('evidence_selection_version')!=SELECTION_VERSION
                    or frozen.get('claim_review_version')!=REVIEW_VERSION
                    or frozen.get('evidence_profile')!=budget_profile(question,max_items=self.settings.question_context_k,
                        context_chars=self.settings.question_context_max_chars)):
                raise RagError(409,'question_snapshot_changed','问答证据选择或复核策略已变化，请创建新任务')
            if (frozen.get("document_id") != document_id or
                    frozen.get("qualifier_cue_version") != QUALIFIER_CUE_VERSION or
                    frozen.get("configuration_signature") != self._configuration_signature()):
                raise RagError(409, "question_snapshot_changed", "问答配置或输入范围已变化，请创建新任务")
            if self.repository.get(document_id) is None:
                raise RagError(404, "document_not_found", "文档不存在")
            info = self.index.verify_snapshot(document_id, frozen)
            start_version_id = frozen["start_version_id"]
            document = SimpleNamespace(filename=frozen["filename"])
            self._verify_snapshot(_IndexSnapshot(
                document_id, frozen["filename"], info["index_id"], info["parse_version_id"],
                info["is_legacy"], start_version_id, info["index_id"]))
            self._verify_table_snapshot(frozen)

        # 固定规则和问题本身已超限时直接报错，不能伪装成“没有证据”。
        memory = frozen.get('conversation_context') if frozen else None
        if frozen and 'conversation_context' in frozen and frozen.get('conversation_policy_signature') != MEMORY_SIGNATURE:
            raise RagError(409, 'conversation_policy_changed', '会话上下文策略已变化，请创建新任务')
        if memory is not None and not valid_memory(memory):
            raise RagError(409, 'conversation_context_invalid', '会话上下文无效，请重新提问')
        from app.query_understanding import understand
        understanding = understand(question,(memory or {}).get("entity_context",{}),self.settings,scope='document')
        search_question = retrieval_question(understanding["search_question"], memory)
        system_prompt = (build_task_system_prompt(include_table_structure=False, question=question) if frozen is not None
                         else build_system_prompt())
        user_prompt_builder = build_task_user_prompt if frozen is not None else build_user_prompt
        if memory:
            system_prompt += MEMORY_RULE
            user_prompt_builder = with_memory_prompt(user_prompt_builder, memory)
        context_cap = self._context_cap(system_prompt, question, document.filename, task_protocol=frozen is not None, memory=memory)
        # 唯一一次查询 embedding + 检索；检索固定版本由 DocumentIndex 返回。
        # 先记住检索前的活动索引，用于识别“检索期间索引被切换”的情况。
        index_before_search = info["index_id"]
        retrieval_started = time.perf_counter()
        if retrieval_checkpoint is not None:
            if frozen is None:
                raise RagError(409, "question_checkpoint_invalid", "恢复检索检查点需要固定问答范围")
            # 检查点由任务仓储提供；恢复仍核对每个块的原文与来源，不能信任任意JSON。
            search_result = self.index.validate_checkpoint(document_id, frozen, retrieval_checkpoint)
        else:
            emit("query_started", {})
            if frozen is None:
                search_result = self.index.search(document_id, question, self.settings.rag_retrieval_k)
            else:
                search_result = self.index.search_snapshot(
                    document_id, search_question, self.settings.rag_retrieval_k, snapshot=frozen,
                    include_scores=self.settings.question_retrieval_strategy == "hybrid-context")
                if self.settings.question_retrieval_strategy == "hybrid-context":
                    from app.retrieval_planning import plan_retrieval
                    search_result = plan_retrieval(search_question, search_result,
                        self.repository.chunks(document_id, frozen["parse_version_id"]),
                        max_candidates=self.settings.question_candidate_k,
                        fulltext_budget=context_cap, max_items=self.settings.question_context_k,
                        payload_transform=task_evidence_payload,
                        reranker=configured_reranker(self.settings,frozen.get('reranker_input_hash')))
            emit("query_completed", {"search_result": search_result,
                                     "provider_usage": getattr(self.index.embedding, "last_call_metadata", None)})
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
        table_blocks = self._verify_table_snapshot(frozen) if frozen is not None else None

        # 证据预算：先反推“参考资料序列化后还能占多少字符”，再整块装入。
        build = build_evidence(search_result, self.settings,
                               max_items=self.settings.question_context_k if frozen is not None
                                   and self.settings.question_retrieval_strategy == "hybrid-context" else None,
                               context_max_chars=self.settings.question_context_max_chars if frozen is not None else None,
                               version_warnings=evidence_version.warnings, context_cap=context_cap,
                               expected_document_id=document_id,
                               payload_transform=task_evidence_payload if frozen is not None else None,
                               table_blocks=table_blocks,
                               table_prompt_overhead=len(build_task_system_prompt(question=question)) - len(system_prompt)
                                   if frozen is not None else 0,
                               include_qualifier_cues=frozen is not None,
                               preserve_document_order=frozen is not None,
                               extractive_question=search_question if frozen is not None
                                   and frozen.get('evidence_selection_version')==SELECTION_VERSION else None)
        if build.fatal_reason:
            # 证据混入其他文档或版本：受控失败，不生成、不拼接。
            raise RagError(409, "evidence_scope_mismatch", build.fatal_reason)
        pack = build.pack
        if any(item.table_projection_included for item in pack.items):
            system_prompt = build_task_system_prompt(question=question) + (MEMORY_RULE if memory else '')
        from app.retrieval_planning import input_coverage_hints
        coverage_hints = input_coverage_hints(search_result.get("planning") or {},
                                             {item.chunk_id for item in pack.items})
        warnings = list(build.warnings)
        limitations = list(build.limitations)
        warnings.extend(self._snapshot_warnings(snapshot))
        emit("evidence_fixed", {"diagnostics": {
            "selected": [{"reference_id": item.reference_id, "chunk_id": item.chunk_id,
                          "page": item.page, "text": item.text, "document_id": item.document_id,
                          "parse_version_id": item.parse_version_id,
                          'read_ranges':item.read_ranges,'segment_policy':item.segment_policy,
                          'unit_choices':item.unit_choices,
                          "table_projection": item.table_projection,
                          "table_projection_included": item.table_projection_included,
                          "table_projection_omitted": item.table_projection_omitted,
                          "qualifier_cues": item.qualifier_cues,
                          "qualifier_cues_included": item.qualifier_cues_included,
                          "sources": [source.model_dump() for source in item.sources]} for item in pack.items],
            "drop_reasons": list(pack.drop_reasons), "context_chars": pack.context_chars,
            "input_chars": len(system_prompt) + len(user_prompt_builder(
                question=question, document_name=document.filename, evidence_payload=pack.payload())),
            "retrieval_timings_ms": dict(search_result.get("timings_ms") or {}),
            "strategy": search_result.get("strategy", "vector"),
            "candidate_count": len(search_result.get("results", [])), "selected_count": len(pack.items),
            "planned_route": (search_result.get("planning") or {}).get("route", "ranked_evidence"),
            "evidence_order_version": TASK_EVIDENCE_ORDER_VERSION if frozen is not None else "relevance-order-v1",
            "input_coverage_hints": coverage_hints,
            "evidence_slot_hints": pack.slot_hints,
            'evidence_selection_version':pack.review_policy,
            'continuation_pairs':pack.continuation_pairs,'budget_profile':pack.budget_profile,
        }})

        if not pack.items:
            # 没有合格证据时直接返回依据不足，不调用 DeepSeek。
            warnings.extend(self._drop_warnings(pack.drop_reasons))
            limitations.append("本次请求没有可送入模型的证据块，因此没有调用生成模型")
            outcome = self._insufficient(
                snapshot, pack, warnings, limitations,
                retrieval_ms=retrieval_ms, started=started, generated=False)
            if frozen is not None:
                outcome.answer.prompt_version = TASK_PROMPT_VERSION
                if frozen.get('answer_contract_version') in {'fact-context-parts-v8','fact-context-parts-v9'}:
                    outcome.answer.presentation={'blocks':[]}
                from app.rag_answer_contract import question_parts
                if frozen.get('answer_contract_version')=='fact-context-parts-v9':
                    from app.question_obligations import answer_question_parts as question_parts
                outcome.answer.answer_parts = [RagAnswerPart(**part, state="missing_evidence", fact_indices=[],
                    missing_detail="本次没有可送入生成模型的合格证据片段。") for part in question_parts(question)]
            return outcome

        if not self.settings.deepseek_api_key.strip():
            raise RagError(503, "provider_not_configured",
                           "尚未配置 DEEPSEEK_API_KEY，请填写本地 .env 后重启服务")

        from copy import deepcopy
        review_pack=deepcopy(pack) if review_on else None
        composition_plan=None
        if frozen is not None:
            from app.rag_composition import prepare_composition, writer_prompt, validate_writer_plan
            def obtain_plan(system,user,validate):
                if composition_checkpoint is not None:return validate(composition_checkpoint)
                if len(system)+len(user)>self.settings.question_input_max_chars:
                    raise RagError(422,'rag_input_too_large','证据规划输入超出预算，未发起调用')
                emit('composition_started',{})
                from app.context_budget import profile_scope
                with profile_scope('planning'):raw_plan=self.model.generate(system,user)
                checkpoint=validate(raw_plan)
                emit('composition_completed',{'checkpoint':checkpoint,'provider_usage':getattr(self.model,'last_call_metadata',None)})
                return checkpoint
            try:pack,composition_plan,_=prepare_composition(pack,question,obtain_plan)
            except ModelOutputError as exc:
                raise RagError(502,'composition_plan_invalid','证据规划未通过核验，未发起正文生成') from None
        user_prompt = user_prompt_builder(question=question, document_name=document.filename,
                                        evidence_payload=pack.payload())
        if frozen is not None:user_prompt=writer_prompt(user_prompt,pack,composition_plan)
        input_limit = self.settings.question_input_max_chars if frozen is not None else self.settings.rag_input_max_chars
        if len(system_prompt) + len(user_prompt) > input_limit:
            raise RagError(422, "rag_input_too_large",
                           "系统规则、问题与固定元数据已超过输入字符预算；"
                           "请缩短问题或调整对应问答输入预算后重试")

        # 一次生成请求；按任务快照选择流式，不重试、不改写、不追加调用。
        generation_started = time.perf_counter()
        if frozen is not None:
            self._verify_table_snapshot(frozen)
        from app.answer_semantic_review import fingerprint as review_fingerprint
        writer_input_hash=review_fingerprint([system_prompt,user_prompt])
        if draft_checkpoint is not None:
            if not review_on or draft_checkpoint.get('input_hash')!=writer_input_hash:
                raise RagError(409,'draft_checkpoint_changed','已保存正文与冻结输入不一致，未重新生成')
            raw=json.dumps(draft_checkpoint['protocol'],ensure_ascii=False)
        else:emit("generation_started", {})
        from app.answer_stream import STREAM_VERSION, FactStreamParser, checked_block
        if draft_checkpoint is not None:pass
        elif frozen and frozen.get('stream_protocol')==STREAM_VERSION and hasattr(self.model,'generate_stream') and observer:
            def completed(category,ordinal,fact):
                block=checked_block(category,ordinal,fact,pack,question,self)
                block['presentation_block']=parser.presentation_block(category,ordinal)
                emit('stream_block',block)
            parser=FactStreamParser(completed)
            emit('stream_started',{'version':STREAM_VERSION})
            last_check=[0.0]
            def stream_active():
                # 不按每个token重算全部源码/库结构；完整论断落盘仍单独核对租约与权限。
                if time.perf_counter()-last_check[0]>=.25:
                    emit('stream_check',{});last_check[0]=time.perf_counter()
            try:
                from app.context_budget import profile_scope
                with profile_scope('answer'):raw=self.model.generate_stream(system_prompt,user_prompt,parser.feed,stream_active)
            except ModelOutputError:
                raise RagError(502,'rag_output_invalid','流式内容未通过结构或引用校验，未发布回答') from None
        else:
            from app.context_budget import profile_scope
            with profile_scope('answer'):raw = self.model.generate(system_prompt, user_prompt)
        generation_ms = _elapsed_ms(generation_started)

        emit("validation_started", {})
        if frozen is not None:
            self._verify_table_snapshot(frozen)
        try:
            if frozen is not None and composition_plan is not None:validate_writer_plan(raw,composition_plan,pack,question=question)
            validated = (validate_task_output(raw, pack, question=question,
                         contract_version=frozen.get("answer_contract_version", "fact-context-parts-v1")) if frozen is not None
                         else validate_model_output(raw, pack))
            validate_user_condition_boundary(validated, pack, question=question)
            from app.rag_support_review import review_claims
            review_claims(validated,pack)
        except ModelOutputError as exc:
            # 只记录非敏感原因码：不带原文、完整输出与凭证。
            logger.warning("RAG 输出校验失败：reason=%s", exc.reason)
            raise RagError(502, "rag_output_invalid",
                           "模型输出未通过结构或引用校验，请重试；本次不会返回未校验内容") from None

        warnings.extend(self._quote_warnings(pack, validated))
        if any(f.support_review.get('risk_codes') for f in validated.conclusion+validated.explanation):
            warnings.append(_warning('claim_support_review_required','warning',
                '部分论断存在结构推断、范围或字面锚点风险，请逐条复核引用；原文匹配不等于语义已证实'))
        warnings.extend(self._post_generation_warnings(snapshot))
        answer = self._build_answer(
            snapshot, pack, validated, question=question, warnings=warnings,
            limitations=limitations, retrieval_ms=retrieval_ms, generation_ms=generation_ms,
            started=started, generated=True)
        if frozen is not None:
            answer.prompt_version = TASK_PROMPT_VERSION
            answer.missing_points = list(validated.missing_points)
            answer.answer_parts = [RagAnswerPart(**part) for part in validated.answer_parts]
        if review_on:
            from app.answer_semantic_review import request as review_request,messages,validate as validate_review,require_accept
            if draft_checkpoint is None:
                from app.rag_validation import parse_model_output
                emit('draft_completed',{'checkpoint':{'input_hash':writer_input_hash,'protocol':parse_model_output(raw)},
                    'provider_usage':getattr(self.model,'last_call_metadata',None)})
            value=review_request(question,review_pack,answer)
            review_system,review_user=messages(value)
            if len(review_system)+len(review_user)>self.settings.question_input_max_chars:
                raise RagError(422,'semantic_review_input_too_large','复核输入超过预算，保留已生成正文，不发起复核或发布')
            try:
                if review_checkpoint is not None:report=validate_review(review_checkpoint,value)
                else:
                    emit('semantic_review_input',{'request':value})
                    emit('semantic_review_started',{})
                    from app.context_budget import profile_scope
                    with profile_scope('review'):report=validate_review(self.model.generate(review_system,review_user),value)
                    emit('semantic_review_completed',{'report':report,'provider_usage':getattr(self.model,'last_call_metadata',None)})
                require_accept(report)
            except ModelOutputError as exc:
                raise RagError(502,exc.reason,'语义复核未通过，未发布回答；原正文和费用记录保留') from None
            answer.quality_review=report
        return RagOutcome(answer=answer, diagnostics={
            "selected": [{"reference_id": item.reference_id, "chunk_id": item.chunk_id,
                          "page": item.page, "text": item.text,
                          "document_id": item.document_id} for item in (review_pack.items if review_on else pack.items)],
            "dropped_candidates": len(pack.drop_reasons),
            "context_chars": pack.context_chars,
            "input_chars": len(system_prompt) + len(user_prompt),
            "drop_reasons": list(pack.drop_reasons),
            "retrieval_reused": retrieval_checkpoint is not None,
            "retrieval_timings_ms": dict(search_result.get("timings_ms") or {}),
            "input_coverage_hints": coverage_hints,
            "evidence_slot_hints": pack.slot_hints,
            'evidence_selection_version':pack.review_policy,
            'continuation_pairs':pack.continuation_pairs,'budget_profile':pack.budget_profile,
            "provider_usage": getattr(self.model, "last_call_metadata", None),
            "context_selection_count": validated.context_selection_count,
        })

    # ------------------------------------------------------------------
    # 快照与预算
    # ------------------------------------------------------------------
    def _verify_table_snapshot(self, snapshot: dict):
        try:
            return verify_table_snapshot(self.repository, snapshot)
        except ValueError:
            raise RagError(409, "question_table_snapshot_changed",
                           "固定版本的表格结构已变化或版本不受支持，请创建新任务") from None

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

    def _context_cap(self, system_prompt: str, question: str, filename: str | None, *, task_protocol: bool = True, memory=None) -> int:
        """反推参考资料可用的字符预算，保证最终消息体不超限。

        预算里显式计入问题、文件名、转义字符、固定包装与输出 schema 的字符数；
        字符预算不等于 token 预算，这里只做本地输入限流。
        """
        builder = build_task_user_prompt if task_protocol else build_user_prompt
        if memory:
            builder = with_memory_prompt(builder, memory)
        empty_user = builder(question=question, document_name=filename,
                                       evidence_payload=[])
        overhead = len(system_prompt) + len(empty_user)
        input_limit = self.settings.question_input_max_chars if task_protocol else self.settings.rag_input_max_chars
        context_limit = self.settings.question_context_max_chars if task_protocol else self.settings.rag_context_max_chars
        if overhead > input_limit:
            raise RagError(422, "rag_input_too_large", "系统规则、问题与元数据超过输入字符预算，请缩短问题或调整配置")
        # empty_user 已包含 [] 两个字符，实际证据列表替换该字段而不是追加。
        available = input_limit - overhead + 2
        return max(min(context_limit, available), 0)

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
    def _citations(self, pack: EvidencePack, quotes: dict[int, str], bindings: dict | None = None) -> list[RagCitation]:
        """引用列表：只包含正文实际使用的引用，按 reference_id 排序。

        文档 ID、chunk ID、页码与来源全部从本次证据映射复制，模型无法提供也无法伪造。
        """
        citations: list[RagCitation] = []
        for ref in sorted(quotes.keys()):
            binding = (bindings or {}).get(ref)
            item = pack.item_by_ref(binding["source_ref"] if binding else ref)
            if item is None:
                continue
            identity = {}
            if binding:
                from app.rag_citation_identity import citation_identity
                identity = citation_identity(item.document_id, item.parse_version_id, item.chunk_id,
                    binding["char_start"], binding["char_end"], quotes[ref])
            citations.append(RagCitation(
                reference_id=ref, document_id=item.document_id, chunk_id=item.chunk_id,
                parse_version_id=item.parse_version_id, page=item.page,
                quote=quotes[ref], sources=list(item.sources),
                source_reference_id=binding["source_ref"] if binding else None, **identity,
                table_structure=item.table_projection if item.table_projection_included else None))
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

        citations = self._citations(pack, validated.quotes, validated.citation_bindings)
        identities = {citation.reference_id: citation.evidence_id for citation in citations}
        def response_fact(fact):
            return RagFact(text=fact.text, refs=sorted(fact.refs), context=fact.context,
                claim_type=fact.claim_type, support_review=fact.support_review,
                evidence_ids=[identities[ref] for ref in sorted(fact.refs) if identities.get(ref)])
        conclusion = [response_fact(fact) for fact in validated.conclusion]
        explanation = [response_fact(fact) for fact in validated.explanation]
        if validated.status == "insufficient_evidence":
            # 该状态由后端提供固定兜底文本，引用与澄清问题为空。
            answer_text = INSUFFICIENT_EVIDENCE_TEXT
            citations: list[RagCitation] = []
            question_list: list[str] = []
            conclusion, explanation = [], []
        else:
            answer_text = render_markdown(validated)
            question_list = list(validated.clarification_questions)

        return RagAnswer(
            answer_id=uuid4().hex,
            status=validated.status,
            answer=answer_text,
            clarification_questions=question_list,
            citations=citations,
            conclusion=conclusion,
            explanation=explanation,
            presentation=validated.presentation,
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
