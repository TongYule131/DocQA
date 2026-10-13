"""全项目审查发现的恢复、取消及幂等边界；全部使用隔离库和假上游。"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.chunking import ChunkingConfig, chunk_document
from app.document_normalizer import normalize_docling_result
from app.main import create_app
from app.parse_worker import ParseWorker
from app.repository import Repository
from app.vector_index import DocumentIndex
from tests.test_parse_pipeline import FakeDocling, docling_payload, upload_pdf


def test_parse_completion_marker_is_written_after_all_payloads(tmp_path, monkeypatch):
    worker = ParseWorker(Settings(data_dir=tmp_path), client=FakeDocling())
    payload = docling_payload()
    normalized = normalize_docling_result("synthetic", payload, "pdf")
    from app import parse_worker
    replace = parse_worker.os.replace

    def fail_document(source, target):
        if Path(target).name == "document.json":
            raise OSError("合成磁盘中断")
        return replace(source, target)

    monkeypatch.setattr(parse_worker.os, "replace", fail_document)
    with pytest.raises(OSError):
        worker._write_result_files("v-synthetic", payload, "合成正文", normalized)
    destination = tmp_path / "parse-results/v-synthetic"
    assert not (destination / "completed.json").exists()
    monkeypatch.setattr(parse_worker.os, "replace", replace)
    worker._write_result_files("v-synthetic", payload, "合成正文", normalized)
    assert all((destination / name).is_file() for name in
               ("completed.json", "document.json", "document.md", "normalized.json"))


@pytest.mark.parametrize("cancel_at", ["fetch", "files"])
@pytest.mark.parametrize("has_old_version", [False, True])
def test_parse_cancel_after_fetch_does_not_publish(tmp_path, cancel_at, has_old_version):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        doc = upload_pdf(client)
        repository = Repository(tmp_path / "docqa.db")
        if has_old_version:
            original = client.post(f"/api/documents/{doc}/parse").json()["task"]
            ParseWorker(settings, repository, FakeDocling()).run_task(original["id"])
        before_version = repository.get(doc).active_parse_version_id
        task = client.post(f"/api/documents/{doc}/parse", json={"force": True}).json()["task"]
        upstream = FakeDocling(result=docling_payload(text="合成取消的新版本"))
        wait = upstream.wait_for_result

        def cancel_after_result(*args, **kwargs):
            result = wait(*args, **kwargs)
            repository.request_cancel(task["id"])
            return result

        worker = ParseWorker(settings, repository, upstream)
        if cancel_at == "fetch":
            upstream.wait_for_result = cancel_after_result
        else:
            write = worker._write_result_files

            def cancel_after_files(*args, **kwargs):
                result = write(*args, **kwargs)
                repository.request_cancel(task["id"])
                return result

            worker._write_result_files = cancel_after_files
        worker.run_task(task["id"])
        assert repository.get(doc).active_parse_version_id == before_version
        assert repository.get_task(task["id"]).status == "needs_attention"
        assert repository.get_task(task["id"]).upstream_task_id == "upstream-1"


def test_parse_success_cannot_be_retried(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        doc = upload_pdf(client)
        task = client.post(f"/api/documents/{doc}/parse").json()["task"]
        repository = Repository(tmp_path / "docqa.db")
        ParseWorker(settings, repository, FakeDocling()).run_task(task["id"])
        response = client.post(f"/api/parse-tasks/{task['id']}/retry")
        assert response.status_code == 409
        assert repository.find_active_task(doc) is None


def test_coalesced_parse_key_stays_bound_after_completion(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        doc = upload_pdf(client)
        first = client.post(f"/api/documents/{doc}/parse",
                            json={"force": True, "idempotency_key": "first"}).json()["task"]
        request = {"force": True, "idempotency_key": "coalesced"}
        repeated = client.post(f"/api/documents/{doc}/parse", json=request).json()["task"]
        assert repeated["id"] == first["id"]
        repository = Repository(tmp_path / "docqa.db")
        ParseWorker(settings, repository, FakeDocling()).run_task(first["id"])
        replay = client.post(f"/api/documents/{doc}/parse", json=request)
        assert replay.json()["task"]["id"] == first["id"]
        assert repository.find_active_task(doc) is None
        conflict = client.post(f"/api/documents/{doc}/parse",
                               json={"force": False, "idempotency_key": "coalesced"})
        assert conflict.status_code == 409


def test_cached_parse_key_binds_successful_version_even_after_failed_reparse(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        doc = upload_pdf(client)
        first = client.post(f"/api/documents/{doc}/parse").json()["task"]
        repository = Repository(tmp_path / "docqa.db")
        ParseWorker(settings, repository, FakeDocling()).run_task(first["id"])
        failed = client.post(f"/api/documents/{doc}/parse", json={"force": True}).json()["task"]
        with repository.connect() as db:
            db.execute("UPDATE parse_tasks SET status='failed' WHERE id=?", (failed["id"],))
        cached = client.post(f"/api/documents/{doc}/parse", json={"idempotency_key": "cached"})
        assert cached.status_code == 200
        assert cached.json()["task"]["id"] == first["id"]
        conflict = client.post(f"/api/documents/{doc}/parse", json={"force": True, "idempotency_key": "cached"})
        assert conflict.status_code == 409
        assert repository.find_active_task(doc) is None


@pytest.mark.parametrize("already_indexed", [False, True])
def test_explicit_historical_index_does_not_report_unchanged_preview_as_changed(tmp_path, already_indexed):
    settings = Settings(data_dir=tmp_path, embedding_api_key="offline-fake")

    class Embedding:
        signature = "synthetic-model"
        calls = 0

        def __init__(self):
            self.settings = settings

        def embed(self, texts):
            self.calls += 1
            return [[1.0, 0.0] for _ in texts]

    with TestClient(create_app(settings)) as client:
        doc = upload_pdf(client)
        repository = Repository(tmp_path / "docqa.db")
        task = client.post(f"/api/documents/{doc}/parse").json()["task"]
        ParseWorker(settings, repository, FakeDocling()).run_task(task["id"])
        old_version = repository.get(doc).active_parse_version_id
        provider = Embedding()
        index = DocumentIndex(repository, provider)
        if already_indexed:
            index.build(doc)
        second = client.post(f"/api/documents/{doc}/parse", json={"force": True}).json()["task"]
        ParseWorker(settings, repository, FakeDocling(result=docling_payload(text="合成第二版正文"))).run_task(second["id"])
        preview = repository.get(doc).active_parse_version_id
        before_calls = provider.calls
        result = index.build(doc, version_id=old_version)
        assert result["status"] == "indexed"
        assert result["parse_version_id"] == old_version
        assert result["matches_active_version"] is False
        assert repository.get(doc).active_parse_version_id == preview
        assert provider.calls == before_calls + (0 if already_indexed else 1)


def test_independent_equal_table_values_survive_preview_and_retrieval_chunks():
    payload = docling_payload()
    data = payload["tables"][0]["data"]
    data["num_cols"] = 3
    for index, row in enumerate(data["grid"]):
        cell = dict(row[-1], text="成本" if index == 0 else "12.5",
                    start_col_offset_idx=2, end_col_offset_idx=3)
        row.append(cell)
    data["table_cells"] = [cell for row in data["grid"] for cell in row]
    normalized = normalize_docling_result("synthetic", payload, "pdf")
    table = next(block for block in normalized.blocks if block.block_type == "table")
    assert "甲区 | 12.5 | 12.5" in table.text
    chunks = chunk_document(normalized, ChunkingConfig())
    assert any("甲区 | 12.5 | 12.5" in chunk.text for chunk in chunks)


def test_retrieval_table_chunks_preserve_merge_spans_and_empty_columns():
    from app.document_normalizer import NormalizedDocument
    from app.schemas import Block

    table = Block(id="t", document_id="synthetic", parse_version_id="", order_index=0,
                  block_type="table", text="", table={"num_rows": 3, "num_cols": 3, "cells": [
                      {"row": 0, "col": 0, "text": "区域", "row_span": 2, "column_header": True},
                      {"row": 0, "col": 1, "text": "收入", "col_span": 2, "column_header": True},
                      {"row": 1, "col": 1, "text": "主营", "column_header": True},
                      {"row": 1, "col": 2, "text": "其它", "column_header": True},
                      {"row": 2, "col": 0, "text": "甲区"},
                      {"row": 2, "col": 2, "text": "12.5"},
                  ]})
    document = NormalizedDocument("synthetic", "docx", 0, blocks=[table])
    text = "\n".join(chunk.text for chunk in chunk_document(document, ChunkingConfig()))
    assert "区域 | 收入 | ↳" in text
    assert "↳ | 主营 | 其它" in text
    assert "甲区 | （空） | 12.5" in text


@pytest.mark.parametrize("span", [2, 1_000_001])
def test_invalid_retrieval_table_geometry_is_rejected_without_overwriting_cells(span):
    from app.document_normalizer import NormalizedDocument
    from app.schemas import Block

    table = Block(id="t", document_id="synthetic", parse_version_id="", order_index=0,
                  block_type="table", text="", table={"cells": [
                      {"row": 0, "col": 0, "text": "甲", "col_span": span},
                      {"row": 0, "col": 1, "text": "独立值"},
                  ]})
    document = NormalizedDocument("synthetic", "docx", 0, blocks=[table])
    with pytest.raises(ValueError, match="重叠|容量"):
        chunk_document(document, ChunkingConfig())


def test_parse_unexpected_payload_error_does_not_log_sensitive_values(tmp_path, caplog):
    settings = Settings(data_dir=tmp_path)
    private = "SENSITIVE-REVIEW-PAYLOAD"
    with TestClient(create_app(settings)) as client:
        doc = upload_pdf(client)
        task = client.post(f"/api/documents/{doc}/parse").json()["task"]
        payload = docling_payload()
        payload["texts"][0]["prov"][0]["charspan"] = [private, 3]
        repository = Repository(tmp_path / "docqa.db")
        ParseWorker(settings, repository, FakeDocling(result=payload)).run_task(task["id"])
        assert repository.get_task(task["id"]).status == "failed"
        assert private not in caplog.text


def test_unexpected_index_error_does_not_log_sensitive_provider_message(tmp_path, caplog):
    from app.embedding import EmbeddingError

    settings = Settings(data_dir=tmp_path, embedding_api_key="offline-fake")
    private = "SENSITIVE-REVIEW-PROVIDER"

    class Embedding:
        signature = "synthetic-model"

        def __init__(self):
            self.settings = settings

        def embed(self, texts):
            raise RuntimeError(private)

    with TestClient(create_app(settings)) as client:
        doc = upload_pdf(client)
        repository = Repository(tmp_path / "docqa.db")
        task = client.post(f"/api/documents/{doc}/parse").json()["task"]
        ParseWorker(settings, repository, FakeDocling()).run_task(task["id"])
        with pytest.raises(EmbeddingError):
            DocumentIndex(repository, Embedding()).build(doc)
        assert private not in caplog.text


def test_docling_retry_backoff_cannot_exceed_remaining_deadline(tmp_path):
    import httpx
    from app.docling_client import DoclingClient, DoclingError, ERROR_DEADLINE

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

        def sleep(self, delay):
            self.now += delay

    clock = Clock()
    settings = Settings(data_dir=tmp_path, docling_total_timeout_seconds=5,
                        docling_read_timeout_seconds=2, docling_retry_backoff_seconds=10)
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with DoclingClient(settings, transport=transport, clock=clock, sleep=clock.sleep) as client:
        with pytest.raises(DoclingError) as error:
            client.wait_for_result("synthetic-task")
        assert error.value.code == ERROR_DEADLINE
        assert clock.now <= 5


def test_analysis_new_plan_after_limit_change_does_not_conflict_with_old_result(tmp_path):
    from dataclasses import replace
    from app.analysis_jobs import AnalysisJobService
    from app.analysis_worker import AnalysisWorker
    from app.schemas import AnalysisJobRequest
    from tests.test_analysis import StubModel, extraction_reply, fixture_settings, seed_document

    settings = fixture_settings(tmp_path, deepseek_api_key="offline-fake", analysis_max_items_per_batch=1)
    repository = Repository(tmp_path / "docqa.db")
    doc, version = seed_document(repository)
    service = AnalysisJobService(settings, repository)
    outcome = service.build_plan(doc, "extraction")
    assert len(outcome.plan.batches) == 1
    ref, unit = next((index, unit) for index, unit in enumerate(outcome.plan.batches[0], 1)
                     if "营业收入为 1.2 亿元" in unit.text)
    model = StubModel([extraction_reply(
        [{"kind": "data", "content": "公司2024年营业收入为1.2亿元", "name": "营业收入",
          "value_text": "1.2 亿元", "refs": [ref]}], {str(ref): unit.text})])
    original, _ = service.submit(doc, "extraction", AnalysisJobRequest(
        parse_version_id=version, plan_fingerprint=outcome.plan.fingerprint, idempotency_key="original"))
    AnalysisWorker(settings, repository, model=model).run_job(original.job.id)
    assert repository.get_analysis_job(original.job.id).status == "succeeded"
    changed = AnalysisJobService(replace(settings, analysis_max_items_per_batch=2), repository)
    plan = changed.build_plan(doc, "extraction")
    assert plan.plan.fingerprint != outcome.plan.fingerprint
    response, status = changed.submit(doc, "extraction", AnalysisJobRequest(
        parse_version_id=version, plan_fingerprint=plan.plan.fingerprint, idempotency_key="changed"))
    assert status == 202 and response.job.id != original.job.id
    assert response.job.plan_fingerprint == plan.plan.fingerprint
    assert len(model.calls) == 1


def test_normalizer_does_not_silently_drop_tail_after_many_repeated_references():
    from app.document_normalizer import normalize_docling_result

    payload = {"body": {"children": [{"$ref": "#/texts/0"}] * 200000 + [{"$ref": "#/texts/1"}]},
               "texts": [{"self_ref": "#/texts/0", "label": "text", "text": "首段"},
                         {"self_ref": "#/texts/1", "label": "text", "text": "必须保留的尾段"}]}
    normalized = normalize_docling_result("doc", payload, "pdf")
    assert [block.text for block in normalized.blocks] == ["首段", "必须保留的尾段"]


def test_encrypted_office_manifest_returns_controlled_format_error():
    import io
    import zipfile
    from app.file_detect import UploadFormatError, detect_format

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "wordprocessingml.document.main+xml")
        archive.writestr("word/document.xml", "<document/>")
    encrypted = bytearray(stream.getvalue())
    # ZIP 的本地与中央目录加密标志；无需密码或外部加密工具。
    encrypted[6] |= 1
    central = encrypted.find(b"PK\x01\x02")
    encrypted[central + 8] |= 1
    with pytest.raises(UploadFormatError) as error:
        detect_format("encrypted.docx", bytes(encrypted))
    assert error.value.status_code == 422


def test_repository_retains_block_sheet_name_after_roundtrip(tmp_path):
    from tests.test_analysis import seed_document

    repository = Repository(tmp_path / "docqa.db")
    _, version = seed_document(repository)
    with repository.connect() as database:
        database.execute("UPDATE blocks SET sheet_name=? WHERE parse_version_id=?", ("销售明细", version))
    assert all(block.sheet_name == "销售明细" for block in repository.blocks(version))


def test_docling_late_success_does_not_fetch_after_deadline(tmp_path):
    import httpx
    from app.docling_client import DoclingClient, DoclingError, ERROR_DEADLINE

    calls = []
    now = [0.0]

    def handle(request):
        calls.append(request.url.path)
        assert request.extensions["timeout"]["read"] <= 5
        now[0] = 6
        return httpx.Response(200, json={"task_status": "success"})

    settings = Settings(data_dir=tmp_path, docling_total_timeout_seconds=5, docling_read_timeout_seconds=2)
    with DoclingClient(settings, transport=httpx.MockTransport(handle), clock=lambda: now[0]) as client:
        with pytest.raises(DoclingError) as error:
            client.wait_for_result("synthetic-task")
    assert error.value.code == ERROR_DEADLINE
    assert calls == ["/v1/status/poll/synthetic-task"]


def test_docling_cancellation_interrupts_retry_backoff(tmp_path):
    import httpx
    from app.docling_client import DoclingClient, DoclingError, ERROR_CANCELLED

    now = [0.0]
    requests = []

    def sleep(delay):
        now[0] += delay

    def handle(request):
        requests.append(request)
        return httpx.Response(503)

    settings = Settings(data_dir=tmp_path, docling_retry_backoff_seconds=10)
    with DoclingClient(settings, transport=httpx.MockTransport(handle),
                       sleep=sleep, clock=lambda: now[0]) as client:
        with pytest.raises(DoclingError) as error:
            client.wait_for_result("synthetic-task", should_stop=lambda: now[0] >= .5)
    assert error.value.code == ERROR_CANCELLED
    assert len(requests) == 1 and now[0] == .5


@pytest.mark.parametrize("module_name", ["scripts.evaluate_rag", "scripts.evaluate_rag_live"])
def test_retired_rag_evaluators_reject_online_before_reading_configuration(tmp_path, monkeypatch, module_name):
    import importlib
    import sys

    evaluator = importlib.import_module(module_name)

    def forbid_configuration(*args, **kwargs):
        raise AssertionError("旧阶段不能读取真实配置后重新获得一轮内存预算")

    monkeypatch.setattr(Settings, "from_env", forbid_configuration)
    monkeypatch.setattr(sys, "argv", [module_name, "--allow-online", "--data-dir", str(tmp_path / "unused")])
    with pytest.raises(SystemExit) as error:
        evaluator.main()
    assert error.value.code == 2
    assert not (tmp_path / "unused").exists()


def test_retired_rag_browser_entry_stops_before_creating_browser_or_evidence(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("需要 Node 验证历史浏览器入口")
    script = Path(__file__).resolve().parents[1] / "scripts/rag_browser_acceptance.mjs"
    output = tmp_path / "unused"
    result = subprocess.run([node, str(script), "--doc", "synthetic", "--out", str(output),
                             "--chrome", "nonexistent-docqa-review-browser.exe"],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=10)
    assert result.returncode == 2
    assert "历史 RAG" in result.stderr
    assert not output.exists()


def test_legacy_migration_with_default_sqlite_rows_preserves_index_metadata(tmp_path):
    import sqlite3
    from app.migrations import run_migrations
    from tests.test_migration_and_versions import build_legacy_db

    path = tmp_path / "docqa.db"
    original = build_legacy_db(path, provider_signature="synthetic-original-model")
    with sqlite3.connect(path, isolation_level=None) as database:
        run_migrations(database, db_path=path, backup_dir=tmp_path / "backups")
        row = database.execute("SELECT status, provider_signature, source_signature, dimension FROM embedding_indexes").fetchone()
        assert row == ("indexed", "synthetic-original-model", original["signature"], 2)
        assert database.execute("SELECT COUNT(*) FROM embedding_vectors_v2").fetchone()[0] == 2


@pytest.mark.parametrize("action", ["extract", "summary"])
def test_analysis_header_key_cannot_bypass_request_length_limit(tmp_path, monkeypatch, action):
    import httpx
    from tests.test_analysis import fixture_settings, seed_document

    settings = fixture_settings(tmp_path, deepseek_api_key="offline-header-fixture", embedding_api_key="")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)

    def forbid_network(*args, **kwargs):
        raise AssertionError("非法请求不能发起模型或其它外部HTTP调用")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbid_network)
    with TestClient(create_app(settings)) as client:
        response = client.post(f"/api/documents/{document_id}/{action}", json={},
                               headers={"Idempotency-Key": "k" * 201})
        assert response.status_code == 422
        assert repository.list_analysis_jobs(document_id) == []
