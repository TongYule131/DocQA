"""服务状态只读查询：识别陈旧／伪造的就绪记录，不触发模型或任务。"""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.runtime import RuntimeGuard, process_start_token


def test_process_start_token_rejects_invalid_pid_and_is_stable_for_self():
    assert process_start_token(True) is None
    assert process_start_token(-1) is None
    assert process_start_token(0) is None
    token = process_start_token(os.getpid())
    assert token and token == process_start_token(os.getpid())


def test_runtime_status_without_workers_is_read_only_and_not_model_probe(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("只读服务状态不应访问模型、解析服务或领取任务")
    monkeypatch.setattr("app.deepseek.DeepSeekModel.generate", unexpected)
    monkeypatch.setattr("app.embedding.APIEmbedding.embed", unexpected)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        before = (tmp_path / "docqa.db").read_bytes()
        for _ in range(3):
            response = client.get("/api/runtime/status")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["web"]["ready"] and body["web"]["running"]
            assert {w["role"] for w in body["workers"]} == {"parse-worker", "analysis-worker"}
            assert all(not w["ready"] and not w["running"] for w in body["workers"])
            assert str(tmp_path) not in response.text and "fingerprint" not in response.text
        assert (tmp_path / "docqa.db").read_bytes() == before
        assert not list((tmp_path / ".local-service").glob("*worker*"))


@pytest.mark.parametrize("damage", ["none", "pid_reused", "old_record", "code", "schema", "directory", "role", "malformed", "boolean_pid"])
def test_runtime_status_validates_worker_identity_before_claiming_ready(tmp_path, damage):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        runtime = RuntimeGuard(tmp_path, "analysis-worker")
        runtime.mark_ready()
        path = tmp_path / ".local-service" / "analysis-worker.ready"
        record = json.loads(path.read_text(encoding="utf-8"))
        if damage == "pid_reused":
            record["process_start_token"] = "synthetic-old-process"
        elif damage == "old_record":
            record.pop("process_start_token")
        elif damage == "code":
            record["code_fingerprint"] = "synthetic-old-code"
        elif damage == "schema":
            record["schema_version"] = 3
        elif damage == "directory":
            record["data_dir"] = "synthetic-other-directory"
        elif damage == "role":
            record["role"] = "parse-worker"
        elif damage == "boolean_pid":
            record["pid"] = True
        path.write_text("{" if damage == "malformed" else json.dumps(record), encoding="utf-8")
        response = client.get("/api/runtime/status")
        assert response.status_code == 200
        worker = next(w for w in response.json()["workers"] if w["role"] == "analysis-worker")
        assert worker["ready"] == (damage == "none")
        assert str(tmp_path) not in response.text


def test_runtime_status_detects_exited_worker_even_when_ready_record_remains(tmp_path, monkeypatch):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        RuntimeGuard(tmp_path, "parse-worker").mark_ready()
        monkeypatch.setattr("app.runtime.process_start_token", lambda pid: None)
        worker = client.get("/api/runtime/status").json()["workers"][0]
        assert not worker["ready"] and not worker["running"]
        assert "退出" in worker["detail"]
