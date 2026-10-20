"""真实对照验收专用累计账本；逐次HTTP预扣，不等同于业务全局费用限额。"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

import httpx


class EvaluationStopped(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


class EvaluationBudget:
    def __init__(self, path: Path, *, limit: int, fingerprint: str):
        if type(limit) is not int or not 1 <= limit <= 40 or not fingerprint:
            raise ValueError("验收上限必须为1～40且范围已冻结")
        self.path, self.limit, self.fingerprint = Path(path), limit, fingerprint
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS approval (id INTEGER PRIMARY KEY CHECK(id=1), max_requests INTEGER NOT NULL, fingerprint TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (sequence INTEGER PRIMARY KEY, role TEXT NOT NULL, context TEXT NOT NULL, request_sha256 TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, settled_at TEXT, elapsed_ms INTEGER, http_status INTEGER, metadata_json TEXT)")
            approval = db.execute("SELECT max_requests,fingerprint FROM approval WHERE id=1").fetchone()
            if approval is None:
                db.execute("INSERT INTO approval VALUES (1,?,?)", (limit, fingerprint))
            elif approval != (limit, fingerprint):
                raise EvaluationStopped("账本的批准上限或冻结范围不一致，不能重置或更换输入继续计费")

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, *, role: str, context: str, content: bytes) -> int:
        if role not in {"index", "query", "generation"} or not context:
            raise EvaluationStopped("外调必须有明确归属")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            approval = db.execute("SELECT max_requests,fingerprint FROM approval WHERE id=1").fetchone()
            if approval != (self.limit, self.fingerprint):
                raise EvaluationStopped("批准记录变化，停止外调")
            if db.execute("SELECT 1 FROM calls WHERE status IN ('intent','uncertain')").fetchone():
                raise EvaluationStopped("存在在途或未确认调用，不自动重发或继续")
            used = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            if used >= self.limit:
                raise EvaluationStopped("已达到本阶段累计请求上限")
            sequence = used + 1
            db.execute("INSERT INTO calls(sequence,role,context,request_sha256,status,started_at) VALUES (?,?,?,?,'intent',?)",
                       (sequence, role, context, sha256(content).hexdigest(), _now()))
            return sequence

    def settle(self, sequence: int, *, status: str, elapsed_ms: int, http_status=None, metadata=None):
        if status not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("invalid_call_status")
        # 只接受经过本模块白名单过滤的供应商元数据，不落盘任意正文或凭证。
        safe = metadata if isinstance(metadata, dict) else {}
        safe = {key: value for key, value in safe.items() if key in {"usage", "usage_status", "finish_reason", "provider_request_id"}}
        safe = _safe_metadata(safe)
        with closing(self.connect()) as db, db:
            changed = db.execute("UPDATE calls SET status=?,settled_at=?,elapsed_ms=?,http_status=?,metadata_json=? WHERE sequence=? AND status='intent'",
                (status, _now(), max(0, elapsed_ms), http_status, json.dumps(safe), sequence)).rowcount
            if changed != 1:
                raise EvaluationStopped("调用记录已结算或丢失，拒绝覆盖")

    def report(self):
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute("SELECT * FROM calls ORDER BY sequence")]
        for row in rows:
            raw_metadata = row.pop("metadata_json")
            row["metadata"] = json.loads(raw_metadata) if raw_metadata else None
        return {"limit": self.limit, "used": len(rows), "remaining": self.limit - len(rows),
                "fingerprint": self.fingerprint, "calls": rows}


def _safe_metadata(body):
    usage = body.get("usage")
    counts = {key: value for key, value in (usage.items() if isinstance(usage, dict) else [])
              if key in {"prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"}
              and type(value) is int and value >= 0}
    result = {"usage": counts or None, "usage_status": "reported" if counts else "unknown"}
    identifier = body.get("provider_request_id")
    if isinstance(identifier, str) and 0 < len(identifier) <= 200 and all(c.isalnum() or c in "-_.:" for c in identifier):
        result["provider_request_id"] = identifier
    if body.get("finish_reason") in {"stop", "length", "content_filter", "tool_calls", "function_call"}:
        result["finish_reason"] = body["finish_reason"]
    return result


class BudgetTransport(httpx.BaseTransport):
    """包围一次真实HTTP发送；embedding每批都计数，网络失败不退回额度。"""
    def __init__(self, budget: EvaluationBudget, *, role: str, context: str, inner=None):
        self.budget, self.role, self.context = budget, role, context
        self.inner = inner or httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        sequence = self.budget.reserve(role=self.role, context=self.context, content=request.read())
        started = time.perf_counter()
        try:
            response = self.inner.handle_request(request)
            response.read()
        except BaseException:
            self.budget.settle(sequence, status="uncertain", elapsed_ms=round((time.perf_counter() - started) * 1000))
            raise
        body = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = {"usage": parsed.get("usage"), "provider_request_id": response.headers.get("x-request-id") or parsed.get("id")}
                choices = parsed.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    body["finish_reason"] = choices[0].get("finish_reason")
        except (ValueError, TypeError):
            pass
        self.budget.settle(sequence, status="succeeded" if response.status_code == 200 else "failed",
            http_status=response.status_code, elapsed_ms=round((time.perf_counter() - started) * 1000), metadata=body)
        return response

    def close(self):
        self.inner.close()
