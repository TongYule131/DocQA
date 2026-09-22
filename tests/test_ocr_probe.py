"""探针汇总只接受实际节点执行事件，避免把会话声明误算为 GPU 推理。"""
from scripts.serve_with_ocr_probe import summarize_profile, package_versions
import importlib.metadata


def test_profile_requires_node_execution():
    events = [
        {"cat": "Session", "args": {"provider": "CUDAExecutionProvider"}},
        {"cat": "Node", "args": {"provider": "CUDAExecutionProvider"}},
        {"cat": "Node", "args": {"provider": "CPUExecutionProvider"}},
        {"cat": "Node", "args": {}},
    ]
    assert summarize_profile(events) == {"CUDAExecutionProvider": 1, "CPUExecutionProvider": 1}
    assert summarize_profile(events[:1]) == {}


def test_slim_distribution_metadata_does_not_break_probe(monkeypatch):
    def version(name):
        if name == "docling":
            raise importlib.metadata.PackageNotFoundError(name)
        return "test-version"
    monkeypatch.setattr(importlib.metadata, "version", version)
    result = package_versions()
    assert result["docling"] is None
    assert result["docling-slim"] == "test-version"
