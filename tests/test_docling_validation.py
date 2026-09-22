"""验证独立客户端的任务流程；模拟 HTTP，不调用真实模型。"""
import json
import sys

import httpx
import pytest

from scripts import validate_docling


@pytest.mark.parametrize("conversion_status,expected_exit,language", [("success", 0, "ch"), ("partial_success", 1, "ch"), ("success", 0, "en")])
def test_async_result_and_partial_are_distinct(tmp_path, monkeypatch, conversion_status, expected_exit, language):
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "中文.pdf").write_bytes(b"sample-placeholder")
    if conversion_status == "partial_success":
        (samples / "后续.pdf").write_bytes(b"must-not-submit")
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/convert/file/async":
            # 验证实际 multipart 包含文件名、内容和明确的 OCR 引擎。
            assert "中文.pdf".encode() in request.content
            assert b"sample-placeholder" in request.content
            assert b"rapidocr" in request.content
            assert ('name="ocr_lang"\r\n\r\n' + language).encode() in request.content
            return httpx.Response(200, json={"task_id": "task-1", "task_status": "pending"})
        if request.url.path.startswith("/v1/status/"):
            return httpx.Response(200, json={"task_id": "task-1", "task_status": "success"})
        if request.url.path.startswith("/v1/result/"):
            return httpx.Response(200, json={"status": conversion_status,
                "document": {"md_content": "中文内容", "json_content": {"pages": {"1": {}}, "tables": []}}})
        return httpx.Response(200, json={})

    real_client = httpx.Client
    monkeypatch.setattr(validate_docling.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setattr(validate_docling.time, "sleep", lambda _: None)
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["validate", str(samples), "--output", str(output), "--ocr-lang", language])
    assert validate_docling.main() == expected_exit
    summary = json.loads(next(output.glob("*/summary.json")).read_text(encoding="utf-8"))
    assert summary[0]["task_status"] == "success"
    assert summary[0]["conversion_status"] == conversion_status
    assert summary[0]["ocr_lang"] == language
    assert next(output.glob("*/*.task.json")).exists()
    assert calls.count("/v1/convert/file/async") == 1


def test_remote_server_is_rejected(tmp_path, monkeypatch):
    # 防止用户误配 URL 后把验证样本发送到远程地址。
    monkeypatch.setattr(sys, "argv", ["validate", str(tmp_path), "--url", "https://example.com"])
    with pytest.raises(SystemExit) as error:
        validate_docling.main()
    assert error.value.code == 2


def test_uncertain_submission_stops_without_duplicate_tasks(tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    samples.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (samples / name).write_bytes(b"sample")
    submissions = []

    def handle(request):
        if request.method == "POST":
            submissions.append(request.url.path)
            # 模拟服务已收到请求，但返回任务编号前连接断开。
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={})

    real_client = httpx.Client
    monkeypatch.setattr(validate_docling.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["validate", str(samples), "--output", str(output)])
    assert validate_docling.main() == 1
    assert len(submissions) == 1
    rows = json.loads(next(output.glob("*/summary.json")).read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert "ReadTimeout" in rows[0]["client_error"]


def test_failed_task_does_not_fetch_missing_result(tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "a.pdf").write_bytes(b"sample")

    def handle(request):
        assert not request.url.path.startswith("/v1/result/"), "失败任务不应领取不存在的结果"
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "failed-task", "task_status": "failure", "error": "parse failed"})
        return httpx.Response(200, json={})

    real_client = httpx.Client
    monkeypatch.setattr(validate_docling.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["validate", str(samples), "--output", str(output)])
    assert validate_docling.main() == 1
    row = json.loads(next(output.glob("*/summary.json")).read_text(encoding="utf-8"))[0]
    assert row["task_status"] == "failure"
    assert row["task_details"]["error"] == "parse failed"
