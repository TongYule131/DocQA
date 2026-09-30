"""分析任务的 API 契约测试：真实路由 + 真实 SDK + MockTransport。

任务书 §10 要求“至少一个完整 API 测试使用真实 SDK + MockTransport”，本文件
用 `httpx.MockTransport` 替换**外部 HTTP 边界**（不是在应用内部替换分析服务），
因此经过的是真实 FastAPI 路由、真实 Pydantic 契约、真实仓储与真实 DeepSeek 适配器。

同时验证 A25（无隐藏调用）：规划、状态、能力、历史与导出接口在整条链路上
都不产生任何模型 HTTP 请求；只有提交后的 worker 执行才会真正调用模型。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.analysis_worker import AnalysisWorker
from app.config import Settings
from app.main import create_app
from app.repository import Repository
from tests.test_analysis import (
    _submit,
    extraction_reply,
    fixture_settings,
    seed_document,
    summary_reply,
)


def completion(content: str) -> dict:
    """构造与 DeepSeek 兼容的聊天补全响应（finish_reason=stop 表示完整返回）。"""
    return {"id": "chatcmpl-test", "object": "chat.completion", "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content,
                                     "reasoning_content": "不应返回的思考内容"}}]}


class Recorder:
    """记录所有真实发往模型端点的请求，供断言“无隐藏调用”。

    只匹配模型 API 主机：这样即便同一个进程里还有其它 HTTP 客户端，
    也不会被误计为“模型调用”。
    """

    def __init__(self, responses: list, *, host: str = "api.deepseek.com"):
        self.responses = list(responses)
        self.host = host
        self.requests: list[httpx.Request] = []

    def matches(self, url: httpx.URL) -> bool:
        return url.host == self.host


def mock_transport(monkeypatch, recorder: Recorder) -> None:
    """只拦截发往模型端点的真实 HTTP 请求，返回可控响应。

    为什么在 `HTTPTransport` 层拦截而不是整体替换 `httpx.Client`：
    Starlette 的 TestClient 自身也使用 httpx，把 Client 的 transport 全部替换会
    连应用请求一起劫持。这里只在最外层的真实网络传输上拦截**模型端点**，
    其余请求（包括 TestClient 访问应用）保持原样；SDK 的请求构造、鉴权头与
    响应解析仍是真实代码路径。
    """
    original = httpx.HTTPTransport.handle_request

    def patched(self, request: httpx.Request):
        if recorder.matches(request.url):
            recorder.requests.append(request)
            index = len(recorder.requests) - 1
            if index >= len(recorder.responses):
                raise AssertionError(
                    f"收到第 {index + 1} 次模型请求，但只预设了 {len(recorder.responses)} 次响应；"
                    "说明发生了意外调用或调用次数与预期不符")
            response = recorder.responses[index]
            if isinstance(response, httpx.Response):
                response.request = request
                return response
            return httpx.Response(200, json=completion(response), request=request)
        return original(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", patched)


def api_settings(tmp_path: Path, **overrides) -> Settings:
    """API 测试配置：批次很小以便用少量单元构造多批场景。"""
    defaults = dict(deepseek_api_key="test-secret", analysis_batch_max_chars=1200,
                    analysis_max_requests=8)
    defaults.update(overrides)
    return fixture_settings(tmp_path, **defaults)


def make_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


# ---------------------------------------------------------------------------
# 规划 / 状态 / 历史 / 导出：全部零模型调用
# ---------------------------------------------------------------------------
def test_read_only_endpoints_never_call_the_model(monkeypatch, tmp_path):
    """A25：规划、能力、状态、历史与导出都不产生任何模型 HTTP 请求。"""
    settings = api_settings(tmp_path)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    recorder = Recorder([])          # 任何请求都会让测试失败
    mock_transport(monkeypatch, recorder)

    with make_client(settings) as client:
        plan = client.post(f"/api/documents/{document_id}/analysis-plan?kind=extraction")
        assert plan.status_code == 200
        body = plan.json()
        assert body["executable"] is True
        assert body["coverage"]["planned_units"] == body["coverage"]["total_units"]
        assert body["request_upper_bound"] >= 1
        assert body["plan_fingerprint"]
        assert client.get("/api/capabilities").status_code == 200
        assert client.get("/api/analysis/status").json()["requires_embedding_index"] is False
        assert client.get(f"/api/documents/{document_id}/analysis-jobs").json() == []
        assert client.get(f"/api/documents/{document_id}/analysis-results").json() == []
        assert client.get("/api/documents").status_code == 200
        assert client.post(f"/api/documents/{document_id}/analysis-plan?kind=summary").status_code == 200
    assert recorder.requests == []


def test_submit_is_idempotent_and_calls_nothing(monkeypatch, tmp_path):
    """A06/A25：提交只写库；重复提交不新增任务，也不产生模型调用。"""
    settings = api_settings(tmp_path)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    recorder = Recorder([])
    mock_transport(monkeypatch, recorder)

    with make_client(settings) as client:
        first = client.post(f"/api/documents/{document_id}/extract",
                            json={"idempotency_key": "api-key-1"})
        assert first.status_code == 202
        job_id = first.json()["job"]["id"]
        again = client.post(f"/api/documents/{document_id}/extract",
                            json={"idempotency_key": "api-key-1"})
        assert again.status_code == 202 and again.json()["job"]["id"] == job_id
        # 无幂等键的重复提交同样复用活动任务。
        third = client.post(f"/api/documents/{document_id}/extract", json={})
        assert third.status_code == 202 and third.json()["job"]["id"] == job_id
        jobs = client.get(f"/api/documents/{document_id}/analysis-jobs").json()
        assert len(jobs) == 1
        calls = client.get(f"/api/analysis-jobs/{job_id}/calls").json()
        assert calls["requests_used"] == 0 and calls["calls"] == []
    assert recorder.requests == []          # 提交与查询都没有触发生成


def test_plan_fingerprint_mismatch_is_conflict(monkeypatch, tmp_path):
    """A06/费用保护：计划指纹不匹配时返回 409，要求重新展示计划。"""
    settings = api_settings(tmp_path)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    recorder = Recorder([])
    mock_transport(monkeypatch, recorder)
    with make_client(settings) as client:
        response = client.post(f"/api/documents/{document_id}/summary",
                               json={"plan_fingerprint": "stale-fingerprint"})
        assert response.status_code == 409
        assert "计划指纹" in response.json()["detail"]
        assert client.get(f"/api/documents/{document_id}/analysis-jobs").json() == []
    assert recorder.requests == []


def test_unknown_fields_and_missing_targets(monkeypatch, tmp_path):
    """A03：未知字段 422、不存在 404、无解析版本 409；三者都不调用模型。"""
    settings = api_settings(tmp_path)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    recorder = Recorder([])
    mock_transport(monkeypatch, recorder)
    with make_client(settings) as client:
        assert client.post(f"/api/documents/{document_id}/extract",
                           json={"system_prompt": "覆盖系统规则"}).status_code == 422
        assert client.post(f"/api/documents/{document_id}/summary",
                           json={"model": "other-model"}).status_code == 422
        assert client.post("/api/documents/missing/extract", json={}).status_code == 404
        assert client.get("/api/analysis-jobs/missing").status_code == 404
        assert client.get("/api/analysis-results/missing").status_code == 404
        assert client.get("/api/analysis-results/missing/export?format=markdown").status_code == 404
    assert recorder.requests == []


def test_export_does_not_call_model(monkeypatch, tmp_path):
    """A23/A25：导出已存结果不重新生成，也不产生模型调用。"""
    settings = api_settings(tmp_path)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    recorder = Recorder([])
    mock_transport(monkeypatch, recorder)
    with make_client(settings) as client:
        # 先发布一个结果（走真实仓储，不调用模型）。
        from app.analysis_jobs import AnalysisJobService

        service = AnalysisJobService(settings, repository)
        response, _ = _submit(service, document_id, "extraction")
        job_id = response.job["id"] if isinstance(response.job, dict) else response.job.id
        from datetime import datetime, timedelta, timezone

        with repository.connect() as db:
            db.execute("UPDATE analysis_jobs SET status='running', lease_token='holder',"
                       " lease_expires_at=? WHERE id=?",
                       ((datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(
                           timespec="microseconds"), job_id))
        assert repository.publish_analysis_result(
            job_id, "holder", result_id="res-api", kind="extraction",
            payload={"items": [], "sections": {"data": "none", "conclusion": "none",
                                               "viewpoint": "none"}, "citations": []},
            coverage={"total_units": 1, "processed_units": 1, "unresolved_units": 0,
                      "unresolved_reasons": {}, "excluded_units": 0, "excluded_reasons": {},
                      "batch_total": 1, "batch_completed": 1, "reduce_completed": False,
                      "complete": True, "resolved_original_refs": 0,
                      "unresolved_original_refs": 0},
            warnings=[], limitations=["导出测试"], prompt_version="docqa-extract-v1",
            protocol_version="docqa-extract-protocol-v1", model_signature="m",
            requests_used=0) is True

        detail = client.get("/api/analysis-results/res-api")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["kind"] == "extraction" and payload["coverage"]["complete"] is True
        history = client.get(f"/api/documents/{document_id}/analysis-results?kind=extraction").json()
        assert [item["id"] for item in history] == ["res-api"]

        markdown = client.get("/api/analysis-results/res-api/export?format=markdown")
        assert markdown.status_code == 200
        assert markdown.headers["content-type"].startswith("text/markdown")
        assert "attachment" in markdown.headers["content-disposition"]
        assert "导出测试" in markdown.text
        exported_json = client.get("/api/analysis-results/res-api/export?format=json")
        parsed = json.loads(exported_json.text)
        assert parsed["result_id"] == "res-api"
        assert parsed["coverage"]["complete"] is True
        # 非法导出格式被拒绝，且不接受任意路径。
        assert client.get("/api/analysis-results/res-api/export?format=csv").status_code == 422
    assert recorder.requests == []


# ---------------------------------------------------------------------------
# 端到端：真实 SDK + MockTransport + 真实 worker
# ---------------------------------------------------------------------------
def test_full_api_flow_with_real_sdk_and_mock_transport(monkeypatch, tmp_path):
    """完整 API 流程：规划 → 提交 → worker 生成 → 状态 → 结果 → 导出。

    模型调用经过真实 SDK，只把传输层换成 MockTransport；断言发送给模型的
    system/user 消息确实包含本次单元与服务端分配的编号，且不含块 ID 或文件路径。
    """
    settings = api_settings(tmp_path)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    from app.analysis_jobs import AnalysisJobService

    service = AnalysisJobService(settings, repository)
    outcome = service.build_plan(document_id, "extraction")
    # 与 worker 一致：批内编号从 1 开始，由服务端在送入模型前分配。
    units = outcome.plan.batches[0]
    target_source = next(unit for unit in units if "营业收入为 1.2 亿元" in unit.text)
    target_ref = units.index(target_source) + 1
    target_text = target_source.text
    reply = extraction_reply(
        [{"kind": "data", "content": "2024 年营业收入为 1.2 亿元",
          "name": "营业收入", "value_text": "1.2 亿元", "period": "2024 年",
          "refs": [target_ref]}],
        {str(target_ref): target_text},
        sections={"data": "present", "conclusion": "none", "viewpoint": "none"})
    recorder = Recorder([reply])
    mock_transport(monkeypatch, recorder)

    with make_client(settings) as client:
        plan = client.post(f"/api/documents/{document_id}/analysis-plan?kind=extraction").json()
        created = client.post(f"/api/documents/{document_id}/extract",
                              json={"plan_fingerprint": plan["plan_fingerprint"],
                                    "idempotency_key": "api-flow"})
        assert created.status_code == 202
        job_id = created.json()["job"]["id"]
        assert created.json()["job"]["status"] == "queued"
        assert recorder.requests == []      # 创建任务不调用模型

        worker = AnalysisWorker(settings, repository, worker_id="api-test-worker")
        assert worker.run_job(job_id) is True
        assert len(recorder.requests) == 1

        # 真实 SDK 发出的请求体里带上了本次单元与分配编号。
        sent = json.loads(recorder.requests[0].content)
        assert sent["model"] == "deepseek-flash"
        user_payload = json.loads(sent["messages"][1]["content"])
        assert user_payload["document_name"] == "合成样本-经营说明.txt"
        refs = [unit["ref"] for unit in user_payload["units"]]
        assert target_ref in refs
        contents = "\n".join(unit["content"] for unit in user_payload["units"])
        assert target_text in contents
        # 模型看不到内部标识：单元载荷里没有块 ID、页码或文件路径。
        assert target_source.block_id not in sent["messages"][1]["content"]
        assert "parse_version_id" not in json.dumps(user_payload, ensure_ascii=False)
        assert "C:\\" not in sent["messages"][1]["content"]

        status = client.get(f"/api/analysis-jobs/{job_id}").json()
        assert status["status"] == "succeeded" and status["result_id"]
        assert status["requests_used"] == 1 and status["max_requests"] >= 1
        assert status["coverage"]["complete"] is True
        assert "reasoning" not in json.dumps(status, ensure_ascii=False)

        # 已完成后重复提交：200 复用结果，不新增调用。
        reused = client.post(f"/api/documents/{document_id}/extract", json={})
        assert reused.status_code == 200 and reused.json()["reused"] is True
        assert reused.json()["job"]["id"] == job_id
        assert len(recorder.requests) == 1

        result = client.get(f"/api/analysis-results/{status['result_id']}").json()
        assert result["extraction"]["items"][0]["value_text"] == "1.2 亿元"
        assert result["extraction"]["sections"]["conclusion"] == "none"
        assert result["extraction"]["citations"][0]["quote"] == target_text
        assert result["quality_warnings"] is not None

        exported = client.get(
            f"/api/analysis-results/{status['result_id']}/export?format=markdown")
        assert exported.status_code == 200
        assert "1.2 亿元" in exported.text
        # 导出与界面都不包含模型思考或原始响应。
        assert "reasoning_content" not in exported.text
        assert "chatcmpl-test" not in exported.text
    assert len(recorder.requests) == 1


def test_upstream_error_maps_to_safe_status(monkeypatch, tmp_path):
    """A18：上游 401 只返回安全失败状态，不泄露密钥、路径或上游原文。"""
    settings = api_settings(tmp_path)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    upstream_error = httpx.Response(401, json={
        "error": {"message": "Invalid API key sk-test-secret", "path": "/home/user/.env"}})
    recorder = Recorder([upstream_error])
    mock_transport(monkeypatch, recorder)

    with make_client(settings) as client:
        created = client.post(f"/api/documents/{document_id}/extract", json={})
        assert created.status_code == 202
        job_id = created.json()["job"]["id"]
        worker = AnalysisWorker(settings, repository, worker_id="api-test-worker")
        assert worker.run_job(job_id) is True
        status = client.get(f"/api/analysis-jobs/{job_id}").json()
        assert status["status"] == "failed"
        assert status["error_code"] == "model_error"
        text = json.dumps(status, ensure_ascii=False)
        assert "sk-test-secret" not in text
        assert "/home/user/.env" not in text
        assert "Invalid API key" not in text
        # 失败调用计入预算并被结算。
        calls = client.get(f"/api/analysis-jobs/{job_id}/calls").json()
        assert calls["requests_used"] == 1
        assert calls["calls"][0]["status"] == "failed"
    assert len(recorder.requests) == 1


def test_missing_key_returns_503_without_calling(monkeypatch, tmp_path):
    """A03/A18：缺生成模型配置时返回 503，且完全不发起任何请求。"""
    settings = api_settings(tmp_path, deepseek_api_key="")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    recorder = Recorder([])
    mock_transport(monkeypatch, recorder)
    with make_client(settings) as client:
        assert client.post(f"/api/documents/{document_id}/extract", json={}).status_code == 503
        assert client.post(f"/api/documents/{document_id}/summary", json={}).status_code == 503
        assert client.post(
            f"/api/documents/{document_id}/analysis-plan?kind=extraction").status_code == 503
        assert client.get(f"/api/documents/{document_id}/analysis-jobs").json() == []
    assert recorder.requests == []
