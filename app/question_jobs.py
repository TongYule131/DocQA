"""可恢复的单文档问答：只在本地创建任务、读取历史和导出已校验结果。

问答有查询向量和生成两种收费操作，因此单独保存任务、调用意图和检查点，
不借用只包含生成请求的分析账本。SQLite 短事务中领取、预扣和发布；事务不跨网络。
"""
from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4

from app.rag_context import RagError
from app.repository import Repository, TaskConflict, lease_expiry_iso, now_iso
from app.schemas import RagAnswer


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(value) -> str:
    return sha256(_dump(value).encode()).hexdigest()


def _request_value(payload, name: str, default=None):
    return payload.get(name, default) if isinstance(payload, dict) else getattr(payload, name, default)


def _markdown_text(text: str) -> str:
    """沿用分析导出的文本化规则，原文换行不成为新的 Markdown 或外部资源。"""
    value = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in "\\`*_{}[]()#+-!|":
        value = value.replace(char, "\\" + char)
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "&#10;")


def _usage_metadata(payload) -> dict:
    """只保存已知用量字段；兼容替换适配器时也不落盘任意上游正文或响应头。"""
    if not isinstance(payload, dict):
        return {"usage": None, "usage_status": "unknown"}
    if isinstance(payload.get("calls"), list):
        return {"calls": [_usage_metadata(call) for call in payload["calls"][:20]]}
    usage = payload.get("usage")
    counts = {name: usage[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens",
        "reasoning_tokens", "cached_tokens") if isinstance(usage, dict) and type(usage.get(name)) is int and usage[name] >= 0}
    result = {"usage": counts or None, "usage_status": "reported" if counts else "unknown"}
    identifier = payload.get("provider_request_id")
    if isinstance(identifier, str) and len(identifier) <= 200 and identifier and all(
            char.isalnum() or char in "-_.:" for char in identifier):
        result["provider_request_id"] = identifier
    if payload.get("finish_reason") in {"stop", "length", "content_filter", "tool_calls", "function_call"}:
        result["finish_reason"] = payload["finish_reason"]
    return result


def _public_diagnostics(payload: dict | None) -> dict:
    """状态端点不返回正文，只展示证据身份、预算和安全阶段耗时。"""
    if not isinstance(payload, dict):
        return {}
    safe = {name: payload[name] for name in ("dropped_candidates", "context_chars", "input_chars",
        "retrieval_reused", "retrieval_timings_ms", "candidate_count", "selected_count", "drop_reasons",
        "strategy", "planned_route") if name in payload}
    if isinstance(payload.get('evidence_slot_hints'), dict):
        hints = payload['evidence_slot_hints']
        safe['evidence_slot_hints'] = {key: hints[key] for key in ('version', 'heuristic_only',
            'semantic_completeness_verified', 'unfilled_count') if key in hints}
    if isinstance(payload.get('budget_profile'),dict):
        safe['budget_profile']={k:payload['budget_profile'][k] for k in ('kind','max_items','target_items','context_chars',
            'excerpt_chars','semantic_completeness_verified') if k in payload['budget_profile']}
    if isinstance(payload.get('evidence_selection_version'),str):safe['evidence_selection_version']=payload['evidence_selection_version']
    safe["selected"] = [{name: item[name] for name in ("reference_id", "chunk_id", "page", "document_id")
                        if name in item} for item in payload.get("selected", []) if isinstance(item, dict)]
    return safe


class QuestionJobStore:
    """问答专用仓储；持久化正文只供业务读取，不进入日志或任务诊断。"""

    def __init__(self, repository: Repository):
        self.repository = repository

    def row(self, job_id: str) -> dict | None:
        with self.repository.connect() as db:
            row = db.execute("SELECT * FROM question_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def key(self, document_id: str, key: str) -> dict | None:
        with self.repository.connect() as db:
            row = db.execute("SELECT * FROM question_request_keys WHERE document_id=? AND idempotency_key=?",
                             (document_id, key)).fetchone()
        return dict(row) if row else None

    def create(self, *, document_id: str, question: str, key: str | None, fingerprint: str,
               snapshot: dict, max_requests: int, request_id: str) -> tuple[str, bool]:
        """幂等键比较与插入同事务，两个并发 POST 不会产生第二个任务。"""
        stamp, job_id = now_iso(), f"qjob-{uuid4().hex}"
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                existing = db.execute("SELECT * FROM question_request_keys WHERE document_id=? AND idempotency_key=?",
                                      (document_id, key)).fetchone()
                if existing:
                    if existing["operation"] != "submit" or existing["request_fingerprint"] != fingerprint:
                        raise TaskConflict("该幂等键已用于不同的问答请求，请更换幂等键")
                    return existing["job_id"], True
            db.execute("""INSERT INTO question_jobs
                (id,document_id,parse_version_id,index_id,question,idempotency_key,request_id,
                 request_fingerprint,snapshot_json,max_requests,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, document_id, snapshot["parse_version_id"], snapshot["index_id"], question,
                 key, request_id, fingerprint, _dump(snapshot), max_requests, stamp, stamp))
            if key:
                db.execute("INSERT INTO question_request_keys VALUES (?,?,?,?,?,?)",
                           (document_id, key, job_id, "submit", fingerprint, stamp))
            db.execute("INSERT INTO question_stage_events(job_id,stage,created_at) VALUES (?,'queued',?)",
                       (job_id, stamp))
        return job_id, False

    @staticmethod
    def _valid(db, job_id: str, token: str) -> bool:
        row = db.execute("SELECT status,lease_token,lease_expires_at FROM question_jobs WHERE id=?",
                         (job_id,)).fetchone()
        return bool(row and token and row["status"] == "running" and row["lease_token"] == token
                    and row["lease_expires_at"] and row["lease_expires_at"] > now_iso())

    def lease_valid(self, job_id: str, token: str) -> bool:
        with self.repository.connect() as db:
            return self._valid(db, job_id, token)

    @staticmethod
    def _close_attempt(db, job_id: str, token: str, status: str, stamp: str,
                       error_code: str | None = None) -> None:
        db.execute("UPDATE question_attempts SET status=?,finished_at=?,error_code=?"
                   " WHERE job_id=? AND lease_token=? AND status='running'",
                   (status, stamp, error_code, job_id, token))

    @staticmethod
    def _recover(db) -> None:
        stamp = now_iso()
        expired = db.execute("SELECT * FROM question_jobs WHERE status='running'"
                             " AND (lease_expires_at IS NULL OR lease_expires_at<=?)", (stamp,)).fetchall()
        for row in expired:
            intent = db.execute("SELECT 1 FROM question_calls WHERE job_id=? AND status='intent'",
                                (row["id"],)).fetchone()
            status = "cancelled" if row["cancel_requested"] else "needs_attention" if intent else "queued"
            code = "cancelled" if row["cancel_requested"] else "call_uncertain" if intent else "lease_expired"
            message = {"cancelled": "任务已取消；在途请求可能继续计费",
                       "call_uncertain": "调用结果不确定，系统不会自动重发；请核实后明确重试，可能重复计费",
                       "lease_expired": "执行者租约过期，已保存的检查点将直接复用"}[code]
            db.execute("UPDATE question_calls SET status='uncertain',error_code='call_uncertain',settled_at=?"
                       " WHERE job_id=? AND status='intent'", (stamp, row["id"]))
            db.execute("UPDATE question_jobs SET status=?,error_code=?,error_message=?,lease_token=NULL,"
                       "lease_expires_at=NULL,finished_at=?,updated_at=? WHERE id=?",
                       (status, code, message, None if status == "queued" else stamp, stamp, row["id"]))
            QuestionJobStore._close_attempt(db, row["id"], row["lease_token"],
                                           "failed" if status == "queued" else status, stamp, code)

    def claim(self, worker_id: str, lease_seconds: int, *, job_id: str | None = None) -> dict | None:
        stamp, token = now_iso(), f"{worker_id}:{uuid4().hex}"
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover(db)
            where, params = ("AND id=?", (job_id,)) if job_id else ("", ())
            row = db.execute("SELECT * FROM question_jobs WHERE status='queued' AND cancel_requested=0 "
                             + where + " ORDER BY created_at,rowid LIMIT 1", params).fetchone()
            if row is None:
                return None
            db.execute("UPDATE question_jobs SET status='running',lease_token=?,lease_expires_at=?,heartbeat_at=?,"
                       "started_at=COALESCE(started_at,?),updated_at=?,attempt_count=attempt_count+1 WHERE id=?",
                       (token, lease_expiry_iso(lease_seconds), stamp, stamp, stamp, row["id"]))
            db.execute("INSERT INTO question_attempts(id,job_id,attempt_no,lease_token,status,started_at)"
                       " VALUES (?,?,?,?,'running',?)",
                       (f"qatt-{uuid4().hex}", row["id"], row["attempt_count"] + 1, token, stamp))
        return self.row(row["id"])

    def renew(self, job_id: str, token: str, lease_seconds: int) -> bool:
        stamp = now_iso()
        with self.repository.connect() as db:
            changed = db.execute("UPDATE question_jobs SET lease_expires_at=?,heartbeat_at=?,updated_at=?"
                                 " WHERE id=? AND lease_token=? AND status='running' AND lease_expires_at>?",
                                 (lease_expiry_iso(lease_seconds), stamp, stamp, job_id, token, stamp))
            return changed.rowcount == 1

    def stage(self, job_id: str, token: str, stage: str, detail: str) -> bool:
        stamp = now_iso()
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._valid(db, job_id, token):
                return False
            db.execute("UPDATE question_jobs SET stage=?,stage_detail=?,updated_at=? WHERE id=?",
                       (stage, detail, stamp, job_id))
            db.execute("INSERT INTO question_stage_events(job_id,stage,created_at) VALUES (?,?,?)",
                       (job_id, stage, stamp))
        return True

    def cancelled(self, job_id: str) -> bool:
        row = self.row(job_id)
        return row is None or bool(row["cancel_requested"])

    def reserve(self, job_id: str, token: str, role: str) -> int | None:
        """每次外调之前预扣。调用账本与总数原子一致；取消或令牌失效不发请求。"""
        if role not in {"embedding", "generation"}:
            raise ValueError("invalid_call_role")
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._valid(db, job_id, token):
                return None
            row = db.execute("SELECT * FROM question_jobs WHERE id=?", (job_id,)).fetchone()
            if row["cancel_requested"] or row["requests_used"] >= row["max_requests"]:
                return None
            pending = db.execute("SELECT 1 FROM question_calls WHERE job_id=? AND status='intent'", (job_id,)).fetchone()
            if pending:
                return None
            number, stamp = row["requests_used"] + 1, now_iso()
            db.execute("UPDATE question_jobs SET requests_used=?,updated_at=? WHERE id=?", (number, stamp, job_id))
            db.execute("INSERT INTO question_calls(id,job_id,sequence_no,role,lease_token,status,started_at)"
                       " VALUES (?,?,?,?,?,'intent',?)", (f"qcall-{uuid4().hex}", job_id, number, role, token, stamp))
        return number

    def checkpoint(self, job_id: str, role: str) -> dict | None:
        with self.repository.connect() as db:
            row = db.execute("SELECT payload_json FROM question_checkpoints WHERE job_id=? AND role=?",
                             (job_id, role)).fetchone()
        return json.loads(row[0]) if row else None

    def save_checkpoint(self, job_id: str, token: str, *, role: str, payload: dict,
                        sequence_no: int | None, elapsed_ms: int = 0, provider_usage=None,
                        diagnostics: dict | None = None) -> bool:
        """检查点与成功结算同事务，避免已收费成功的查询因进程退出而重复执行。"""
        serialized = _dump(payload)
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._valid(db, job_id, token):
                return False
            if sequence_no is not None:
                expected_role = "embedding" if role == "retrieval" else "generation"
                call = db.execute("SELECT status,role,lease_token FROM question_calls WHERE job_id=? AND sequence_no=?",
                                  (job_id, sequence_no)).fetchone()
                if call is None or call["status"] != "intent" or call["lease_token"] != token or call["role"] != expected_role:
                    return False
            stamp = now_iso()
            db.execute("INSERT INTO question_checkpoints VALUES (?,?,?,?) ON CONFLICT(job_id,role) DO NOTHING",
                       (job_id, role, serialized, stamp))
            if diagnostics is not None:
                previous = db.execute("SELECT payload_json FROM question_checkpoints WHERE job_id=? AND role='evidence'",
                                      (job_id,)).fetchone()
                if previous:
                    existing = json.loads(previous[0])
                    merged = {**existing, **diagnostics}
                    # outcome 的安全诊断可能只含片段身份；保留发起生成前固定的完整来源映射。
                    prior_selected = {entry.get("chunk_id"): entry for entry in existing.get("selected", [])}
                    if "selected" in diagnostics:
                        merged["selected"] = [{**prior_selected.get(entry.get("chunk_id"), {}), **entry}
                                              for entry in diagnostics["selected"]]
                    diagnostics = merged
                db.execute("INSERT INTO question_checkpoints VALUES (?,'evidence',?,?)"
                           " ON CONFLICT(job_id,role) DO UPDATE SET payload_json=excluded.payload_json",
                           (job_id, _dump(diagnostics), stamp))
            if sequence_no is not None:
                db.execute("UPDATE question_calls SET status='succeeded',settled_at=?,elapsed_ms=?,provider_usage_json=?"
                           " WHERE job_id=? AND sequence_no=? AND lease_token=? AND status='intent'",
                           (stamp, max(0, elapsed_ms), _dump(_usage_metadata(provider_usage)), job_id, sequence_no, token))
        return True

    def finish(self, job_id: str, token: str, *, status: str, code: str, message: str,
               call_status: str = "failed", elapsed_ms: int | None = None, provider_usage=None) -> bool:
        stamp = now_iso()
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._valid(db, job_id, token):
                return False
            row = db.execute("SELECT cancel_requested FROM question_jobs WHERE id=?", (job_id,)).fetchone()
            if row[0]:
                status, code, message = "cancelled", "cancelled", "已取消后续处理；在途请求可能继续计费"
            db.execute("UPDATE question_calls SET status=?,error_code=?,settled_at=?,elapsed_ms=?,provider_usage_json=?"
                       " WHERE job_id=? AND lease_token=? AND status='intent'",
                       (call_status, code, stamp, elapsed_ms, _dump(_usage_metadata(provider_usage)), job_id, token))
            db.execute("UPDATE question_jobs SET status=?,error_code=?,error_message=?,finished_at=?,updated_at=?,"
                       "lease_token=NULL,lease_expires_at=NULL WHERE id=?", (status, code, message, stamp, stamp, job_id))
            self._close_attempt(db, job_id, token, status, stamp, code)
        return True

    def publish(self, job_id: str, token: str, answer: dict) -> bool:
        validated = RagAnswer.model_validate(answer)
        stamp, result_id = now_iso(), f"qres-{job_id}"
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._valid(db, job_id, token):
                return False
            row = db.execute("SELECT * FROM question_jobs WHERE id=?", (job_id,)).fetchone()
            if row["cancel_requested"]:
                return False
            if (validated.document_id, validated.parse_version_id, validated.index_id) != (
                    row["document_id"], row["parse_version_id"], row["index_id"]):
                raise ValueError("answer_scope_mismatch")
            # 引用位置由 RAG 服务校验；发布再确认文档和不可变版本身份，避免错接其他任务结果。
            if any(c.document_id != row["document_id"] or c.parse_version_id != row["parse_version_id"]
                   for c in validated.citations):
                raise ValueError("citation_scope_mismatch")
            evidence_row = db.execute("SELECT payload_json FROM question_checkpoints WHERE job_id=? AND role='evidence'",
                                      (job_id,)).fetchone()
            evidence = json.loads(evidence_row[0]) if evidence_row else {}
            selected = {item["reference_id"]: item for item in evidence.get("selected", [])}
            citation_ids = {citation.reference_id for citation in validated.citations}
            if len(citation_ids) != len(validated.citations):
                raise ValueError("duplicate_result_reference")
            for citation in validated.citations:
                source = selected.get(citation.source_reference_id if citation.source_reference_id is not None else citation.reference_id)
                if (source is None or source.get("chunk_id") != citation.chunk_id or not citation.quote
                        or citation.quote not in str(source.get("text", "")).replace("\r\n", "\n").replace("\r", "\n")):
                    raise ValueError("result_quote_not_in_evidence")
                if "sources" in source and [location.model_dump() for location in citation.sources] != source["sources"]:
                    raise ValueError("result_source_not_in_evidence")
                if citation.evidence_id is not None:
                    from app.rag_citation_identity import citation_identity
                    text = str(source.get("text", "")).replace("\r\n", "\n")
                    if (type(citation.char_start) is not int or type(citation.char_end) is not int
                            or not 0 <= citation.char_start < citation.char_end <= len(text)
                            or text[citation.char_start:citation.char_end] != citation.quote):
                        raise ValueError("result_citation_range_mismatch")
                    expected = citation_identity(citation.document_id, citation.parse_version_id, citation.chunk_id,
                        citation.char_start, citation.char_end, citation.quote)
                    if any(getattr(citation, key) != value for key, value in expected.items()):
                        raise ValueError("result_citation_identity_mismatch")
            snapshot = json.loads(row["snapshot_json"])
            from app.rag_prompts import TASK_PROMPT_VERSION
            from app.rag_extract_context import SELECTION_VERSION
            from app.rag_support_review import REVIEW_VERSION
            if snapshot.get('task_prompt_version')==TASK_PROMPT_VERSION and (
                    snapshot.get('evidence_selection_version')!=SELECTION_VERSION or snapshot.get('claim_review_version')!=REVIEW_VERSION):
                raise ValueError('admission_policy_changed')
            if snapshot.get('evidence_selection_version'):
                from app.rag_support_review import restore_admission,review_claims
                from types import SimpleNamespace
                retrieval_row=db.execute("SELECT payload_json FROM question_checkpoints WHERE job_id=? AND role='retrieval'",(job_id,)).fetchone()
                if retrieval_row is None:raise ValueError('admission_retrieval_missing')
                admission=restore_admission(self.repository,snapshot,evidence.get('selected',[]),json.loads(retrieval_row[0]),row['question'])
                rechecked=validated.model_copy(deep=True)
                reviewed=SimpleNamespace(conclusion=rechecked.conclusion,explanation=rechecked.explanation,
                    quotes={c.reference_id:c.quote for c in rechecked.citations},
                    citation_bindings={c.reference_id:dict(source_ref=c.source_reference_id,char_start=c.char_start,char_end=c.char_end)
                                       for c in rechecked.citations})
                review_claims(reviewed,admission)
                if any(a.support_review!=b.support_review for a,b in zip(
                        rechecked.conclusion+rechecked.explanation,validated.conclusion+validated.explanation)):
                    raise ValueError('claim_review_checkpoint_changed')
            if snapshot.get("answer_contract_version") in {"fact-context-parts-v4","fact-context-parts-v5","fact-context-parts-v6","fact-context-parts-v7"}:
                if any(not citation.evidence_id or citation.source_reference_id is None for citation in validated.citations):
                    raise ValueError("result_citation_identity_missing")
                if len({c.evidence_id for c in validated.citations}) != len(validated.citations):
                    raise ValueError("duplicate_result_evidence_identity")
                identities = {c.reference_id: c.evidence_id for c in validated.citations}
                if any(f.claim_type == "unclassified" or f.evidence_ids != [identities.get(ref) for ref in sorted(f.refs)]
                       for f in validated.conclusion + validated.explanation):
                    raise ValueError("result_claim_evidence_mismatch")
            if any(not fact.refs or not set(fact.refs) <= citation_ids for fact in validated.conclusion + validated.explanation):
                raise ValueError("result_reference_missing")
            snapshot = json.loads(row["snapshot_json"])
            if snapshot.get("table_projection_version"):
                from app.rag_table_projection import validate_table_checkpoint
                validate_table_checkpoint(self.repository, snapshot, evidence.get("selected", []), validated.citations)
            elif any(c.table_structure is not None for c in validated.citations):
                raise ValueError("legacy_result_with_table_projection")
            if snapshot.get("answer_contract_version"):
                # 重启恢复的answer检查点也重核扩展协议，不能只信任字段已落库。
                from app.rag_answer_contract import validate_task_output
                from app.rag_context import EvidenceItem, EvidencePack
                from app.rag_validation import render_markdown, validate_user_condition_boundary
                from app.rag_prompts import INSUFFICIENT_EVIDENCE_TEXT
                is_v4 = snapshot["answer_contract_version"] in {"fact-context-parts-v4","fact-context-parts-v5","fact-context-parts-v6","fact-context-parts-v7"}
                # v4发布时已逐条重算稳定ID和原文范围；用这些已核对范围重验数值、限定和覆盖。
                # 不能把展示引述号再当输入片段号，也不能降级后直接相信落库的答案正文。
                scoped_items = ([EvidenceItem(reference_id=c.reference_id, chunk_id=c.chunk_id,
                    document_id=c.document_id, parse_version_id=c.parse_version_id, page=c.page, text=c.quote,
                    heading_path=admission.item_by_ref(c.source_reference_id).heading_path if snapshot.get('evidence_selection_version') else None)
                    for c in validated.citations] if is_v4 else [EvidenceItem(reference_id=ref, chunk_id=item["chunk_id"],
                    document_id=row["document_id"], parse_version_id=row["parse_version_id"],
                    page=item.get("page"), text=item["text"]) for ref,item in selected.items()])
                pack = EvidencePack(items=scoped_items, candidates=[],
                    index_id=row["index_id"], parse_version_id=row["parse_version_id"], truncated=False, context_chars=0)
                payload = {name: getattr(validated, name) for name in ("status", "clarification_questions", "missing_points")}
                payload.update(conclusion=[f.model_dump(include={"text", "refs", "context"}) for f in validated.conclusion],
                    explanation=[f.model_dump(include={"text", "refs", "context"}) for f in validated.explanation],
                    evidence_quotes=[{"ref":c.reference_id,"quote":c.quote} for c in validated.citations],
                    answer_parts=[{k:v for k,v in p.model_dump().items() if k!="text"} for p in validated.answer_parts])
                own_sources=None
                if is_v4 and snapshot.get('evidence_selection_version'):
                    from app.rag_extract_context import merge_ranges
                    by_citation={c.reference_id:c for c in validated.citations};own_sources=[]
                    for fact in validated.conclusion+validated.explanation:
                        grouped={}
                        for ref in fact.refs:
                            c=by_citation[ref]
                            grouped.setdefault(c.source_reference_id,[]).append([c.char_start,c.char_end])
                        own_sources.append([admission.item_by_ref(ref).text[a:b] for ref,ranges in grouped.items()
                                            for a,b in merge_ranges(ranges)])
                checked = validate_task_output(_dump(payload), pack, question=row["question"],
                                               contract_version="fact-context-parts-v3" if is_v4 else snapshot["answer_contract_version"],
                                               _claim_sources=own_sources,
                                               _section_headings=[item.heading_path for item in admission.items] if is_v4 and snapshot.get('evidence_selection_version') else None)
                for fact, stored in zip(checked.conclusion + checked.explanation, validated.conclusion + validated.explanation):
                    fact.claim_type = stored.claim_type
                    fact.support_review = stored.support_review
                if is_v4 and snapshot.get('evidence_selection_version'):
                    checked.citation_bindings={c.reference_id:dict(source_ref=c.source_reference_id,char_start=c.char_start,char_end=c.char_end)
                                               for c in validated.citations}
                    validate_user_condition_boundary(checked,admission,question=row['question'])
                else:validate_user_condition_boundary(checked, pack, question=row["question"])
                expected_text = (INSUFFICIENT_EVIDENCE_TEXT if validated.status == "insufficient_evidence"
                                 else render_markdown(checked))
                if ([p.model_dump() for p in validated.answer_parts] != checked.answer_parts
                        or validated.prompt_version != snapshot.get("task_prompt_version")
                        or validated.answer != expected_text):
                    raise ValueError("result_contract_mismatch")
            if validated.status in {"answered", "partial"} and not validated.conclusion:
                raise ValueError("answered_without_conclusion")
            if (validated.status == "partial") != bool(validated.missing_points):
                raise ValueError("partial_missing_points_mismatch")
            if validated.status == "insufficient_evidence" and (validated.citations or validated.conclusion or validated.explanation):
                raise ValueError("insufficient_answer_with_facts")
            pending = db.execute("SELECT 1 FROM question_calls WHERE job_id=? AND status='intent'", (job_id,)).fetchone()
            if pending:
                return False
            db.execute("""INSERT INTO question_results
                (id,job_id,document_id,parse_version_id,index_id,question,answer_json,requests_used,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (result_id, job_id, row["document_id"], row["parse_version_id"],
                row["index_id"], row["question"], _dump(validated.model_dump()), row["requests_used"], stamp))
            db.execute("UPDATE question_jobs SET status='succeeded',stage='done',stage_detail='已保存已校验回答',"
                       "result_id=?,error_code=NULL,error_message=NULL,finished_at=?,updated_at=?,"
                       "lease_token=NULL,lease_expires_at=NULL WHERE id=?", (result_id, stamp, stamp, job_id))
            db.execute("INSERT INTO question_stage_events(job_id,stage,created_at) VALUES (?,'done',?)", (job_id, stamp))
            self._close_attempt(db, job_id, token, "succeeded", stamp)
        return True


class QuestionJobService:
    """HTTP 只调用本服务；提交和查询都不执行 embedding 或生成。"""

    def __init__(self, settings, repository: Repository, rag):
        self.settings, self.repository, self.rag = settings, repository, rag
        self.store = QuestionJobStore(repository)

    def submit(self, document_id: str, payload, *, request_id: str | None = None) -> tuple[dict, int]:
        question = _request_value(payload, "question", "")
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise RagError(422, "invalid_question", "问题不能为空且不能超过4000字符")
        key = (_request_value(payload, "idempotency_key") or "").strip() or None
        if key and len(key) > 200:
            raise RagError(422, "invalid_idempotency_key", "幂等键不能超过200字符")
        fingerprint = _fingerprint({"question": question})
        # 重发只读取旧任务，不受此时索引切换或模型配置变化影响。
        if key:
            existing = self.store.key(document_id, key)
            if existing:
                if existing["operation"] != "submit" or existing["request_fingerprint"] != fingerprint:
                    raise TaskConflict("该幂等键已用于不同的问答请求，请更换幂等键")
                job = self.get_job(existing["job_id"])
                return {"job": job, "reused": True}, 200 if job["status"] not in {"queued", "running"} else 202
        snapshot = self.rag.capture_snapshot(document_id, question)
        if (snapshot.get("document_id") != document_id or not snapshot.get("index_id")
                or not snapshot.get("parse_version_id")):
            raise RagError(409, "invalid_question_snapshot", "问答输入快照无效，请重新建立有效索引")
        maximum = getattr(self.settings, "question_max_requests", 2)
        if type(maximum) is not int or maximum < 2 or maximum > 20:
            raise RagError(503, "invalid_question_budget", "问答请求预算配置无效")
        job_id, reused = self.store.create(document_id=document_id, question=question, key=key,
            fingerprint=fingerprint, snapshot=snapshot, max_requests=maximum,
            request_id=request_id or f"req-{uuid4().hex}")
        job = self.get_job(job_id)
        return {"job": job, "reused": reused}, 200 if job["status"] not in {"queued", "running"} else 202

    def get_job(self, job_id: str) -> dict:
        row = self.store.row(job_id)
        if row is None:
            raise RagError(404, "question_job_not_found", "问答任务不存在")
        # 快照和检查点包含证据正文；普通状态轮询只提供安全诊断摘要。
        excluded = {"snapshot_json", "request_fingerprint", "lease_token", "lease_expires_at", "heartbeat_at"}
        job = {key: value for key, value in row.items() if key not in excluded}
        job["cancel_requested"] = bool(job["cancel_requested"])
        with self.repository.connect() as db:
            calls = db.execute("SELECT sequence_no,role,status,error_code,started_at,settled_at,elapsed_ms,provider_usage_json"
                               " FROM question_calls WHERE job_id=? ORDER BY sequence_no", (job_id,)).fetchall()
            stages = db.execute("SELECT stage,created_at FROM question_stage_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
            completed = db.execute("SELECT COUNT(*) FROM question_checkpoints WHERE job_id=? AND role IN ('retrieval','answer')", (job_id,)).fetchone()[0]
        job["calls"] = []
        for call in calls:
            entry = dict(call)
            usage = entry.pop("provider_usage_json")
            entry["provider_usage"] = json.loads(usage) if usage else {"usage": None, "usage_status": "unknown"}
            job["calls"].append(entry)
        job["diagnostics"] = _public_diagnostics(self.store.checkpoint(job_id, "evidence"))
        job["stage_history"] = [dict(stage) for stage in stages]
        job["steps_completed"], job["steps_total"] = completed, 2
        job["requests_remaining"] = row["max_requests"] - row["requests_used"]
        document = self.repository.get(row["document_id"])
        job["is_old_version"] = bool(document and document.active_parse_version_id != row["parse_version_id"])
        return job

    def list_jobs(self, document_id: str) -> list[dict]:
        if self.repository.get(document_id) is None:
            raise RagError(404, "document_not_found", "文档不存在")
        with self.repository.connect() as db:
            ids = db.execute("SELECT id FROM question_jobs WHERE document_id=? ORDER BY created_at DESC,rowid DESC",
                             (document_id,)).fetchall()
        return [self.get_job(row[0]) for row in ids]

    def cancel_job(self, job_id: str, payload=None) -> dict:
        self.get_job(job_id)
        stamp = now_iso()
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM question_jobs WHERE id=?", (job_id,)).fetchone()
            if row[0] in {"queued", "running"}:
                db.execute("UPDATE question_jobs SET cancel_requested=1,updated_at=?,"
                           "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,"
                           "finished_at=CASE WHEN status='queued' THEN ? ELSE finished_at END,"
                           "error_code=CASE WHEN status='queued' THEN 'cancelled' ELSE error_code END,"
                           "error_message=CASE WHEN status='queued' THEN '任务在调用前被取消' ELSE error_message END WHERE id=?",
                           (stamp, stamp, job_id))
        return self.get_job(job_id)

    def retry_job(self, job_id: str, payload=None, *, idempotency_key: str | None = None) -> dict:
        job = self.get_job(job_id)
        key = (idempotency_key or _request_value(payload, "idempotency_key") or "").strip() or None
        if key and len(key) > 200:
            raise RagError(422, "invalid_idempotency_key", "幂等键不能超过200字符")
        fingerprint, stamp = _fingerprint({"retry_job_id": job_id}), now_iso()
        with self.repository.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                existing = db.execute("SELECT * FROM question_request_keys WHERE document_id=? AND idempotency_key=?",
                                      (job["document_id"], key)).fetchone()
                if existing:
                    if existing["operation"] != "retry" or existing["job_id"] != job_id or existing["request_fingerprint"] != fingerprint:
                        raise TaskConflict("该幂等键已用于不同操作，请更换幂等键")
                    # 重复重试不能把后来再次失败的任务重新放回队列。
                    return self.get_job(job_id)
            row = db.execute("SELECT * FROM question_jobs WHERE id=?", (job_id,)).fetchone()
            if row["status"] not in {"failed", "needs_attention"}:
                if row["status"] in {"queued", "running", "succeeded"}:
                    return self.get_job(job_id)
                raise TaskConflict("已取消的任务不能重试，请重新提交问题")
            roles = {r[0] for r in db.execute("SELECT role FROM question_checkpoints WHERE job_id=?", (job_id,))}
            required = 0 if "answer" in roles else 1 if "retrieval" in roles else 2
            if row["max_requests"] - row["requests_used"] < required:
                raise TaskConflict("问答任务剩余预算不足；重试不会重置预算，请查看调用记录")
            if key:
                db.execute("INSERT INTO question_request_keys VALUES (?,?,?,?,?,?)",
                           (job["document_id"], key, job_id, "retry", fingerprint, stamp))
            db.execute("UPDATE question_calls SET error_code='retry_acknowledged_uncertain'"
                       " WHERE job_id=? AND status='uncertain'", (job_id,))
            db.execute("UPDATE question_jobs SET status='queued',stage='queued',stage_detail='明确重试，复用已有检查点',"
                       "error_code=NULL,error_message=NULL,finished_at=NULL,lease_token=NULL,lease_expires_at=NULL,"
                       "cancel_requested=0,last_retry_key=?,updated_at=? WHERE id=?", (key, stamp, job_id))
            db.execute("INSERT INTO question_stage_events(job_id,stage,created_at) VALUES (?,'queued',?)", (job_id, stamp))
        return self.get_job(job_id)

    def get_result(self, result_id: str) -> dict:
        with self.repository.connect() as db:
            row = db.execute("SELECT * FROM question_results WHERE id=?", (result_id,)).fetchone()
        if row is None:
            raise RagError(404, "question_result_not_found", "问答结果不存在")
        result = {key: value for key, value in dict(row).items() if key != "answer_json"}
        result["answer"] = RagAnswer.model_validate_json(row["answer_json"]).model_dump()
        document = self.repository.get(row["document_id"])
        result["is_old_version"] = bool(document and document.active_parse_version_id != row["parse_version_id"])
        return result

    def export(self, result_id: str, fmt: str) -> tuple[str, str, str]:
        from app.rag_answer_contract import fact_display_text
        if fmt not in {"markdown", "json"}:
            raise RagError(422, "unsupported_export_format", "仅支持 markdown 或 json 导出")
        result = self.get_result(result_id)
        if fmt == "json":
            return json.dumps(result, ensure_ascii=False, indent=2), "application/json", f"{result_id}.json"
        answer = result["answer"]
        lines = ["# 问答结果", "", _markdown_text(result["question"]), ""]
        for name, title in (("conclusion", "结论"), ("explanation", "依据与说明")):
            if answer[name]:
                lines += [f"## {title}", ""]
                lines += ["- " + _markdown_text(fact_display_text(fact)) + "".join(f"[{ref}]" for ref in fact["refs"])
                          for fact in answer[name]] + [""]
        if answer.get("answer_parts"):
            labels = {"answered": "已回答", "partial": "部分回答", "missing_evidence": "缺少依据", "needs_clarification": "需要澄清"}
            lines += ["## 问题覆盖", ""] + ["- " + _markdown_text(part["text"]) + "：" + labels[part["state"]]
                + ("；" + _markdown_text(part["missing_detail"]) if part["missing_detail"] else "")
                for part in answer["answer_parts"]] + [""]
        if not answer["conclusion"] and not answer["explanation"]:
            lines += [_markdown_text(answer["answer"]), ""]
        if answer["missing_points"]:
            lines += ["## 本次尚缺少的依据", ""] + ["- " + _markdown_text(text)
                for text in answer["missing_points"]] + [""]
        if answer["clarification_questions"]:
            lines += ["## 需要补充的信息", ""] + ["- " + _markdown_text(text)
                for text in answer["clarification_questions"]] + [""]
        lines += ["## 引用与来源", ""]
        for citation in answer["citations"]:
            labels = []
            for source in citation["sources"]:
                if source.get("page") is not None:
                    labels.append(f"第 {source['page']} 页")
                if source.get("sheet_name"):
                    labels.append(str(source["sheet_name"]) + " " + str(source.get("cell_range") or ""))
                if source.get("section_path"):
                    labels.append(str(source["section_path"]))
            lines += [f"[{citation['reference_id']}] {_markdown_text(' / '.join(labels))}", "", _markdown_text(citation["quote"]), ""]
            if citation.get("evidence_id"):
                lines += [f"证据ID：{citation['evidence_id']}",
                    f"解析原文范围：[{citation['char_start']}, {citation['char_end']}) · Unicode字符，CRLF→LF",
                    "校验：原文精确匹配；语义支持未自动认证。页码/坐标为分块来源区域。", ""]
        lines += [f"解析版本：{_markdown_text(result['parse_version_id'])} · 索引：{_markdown_text(result['index_id'])}",
                  f"任务：{_markdown_text(result['job_id'])} · 请求数：{result['requests_used']}", ""]
        if answer["limitations"]:
            lines += ["## 限制", ""] + [f"- {_markdown_text(text)}" for text in answer["limitations"]] + [""]
        return "\n".join(lines), "text/markdown; charset=utf-8", f"{result_id}.md"
