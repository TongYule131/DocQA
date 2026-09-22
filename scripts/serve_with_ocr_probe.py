"""可选的服务内 OCR 探针：记录真实 HTTP 解析所创建的 ORT 会话及首次推理。

只用于验收，启用 profiling 会影响耗时，不得用该轮结果作性能基准。
不提前导入 torch、不替换模型、不改变 providers，只为 RapidOcr 目录中的会话开启 profiling。
"""
import hashlib
import importlib.metadata
import json
import os
import sys
import threading
import uuid
from pathlib import Path


def summarize_profile(events):
    """汇总实际节点执行后端；声明可用后端或分配显存都不构成执行证据。"""
    counts = {}
    for event in events:
        provider = event.get("args", {}).get("provider")
        if event.get("cat") == "Node" and provider:
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def package_versions():
    # 精简镜像可能只安装 docling-slim；元数据缺失不应中断文档解析。
    versions = {}
    for name in ("docling", "docling-slim", "rapidocr", "onnxruntime-gpu"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def install_probe(output):
    import onnxruntime as ort

    output.mkdir(parents=True, exist_ok=True)
    original_init = ort.InferenceSession.__init__
    original_run = ort.InferenceSession.run

    def traced_init(session, path_or_bytes, sess_options=None, *args, **kwargs):
        path = Path(path_or_bytes) if isinstance(path_or_bytes, (str, os.PathLike)) else None
        monitored = path is not None and "rapidocr" in str(path).lower() and path.is_file()
        if monitored:
            identity = uuid.uuid4().hex
            prefix = output / identity
            sess_options = sess_options if sess_options is not None else ort.SessionOptions()
            sess_options.enable_profiling = True
            sess_options.profile_file_prefix = str(prefix)
        original_init(session, path_or_bytes, sess_options, *args, **kwargs)
        if monitored:
            # 每个模型单独记录，即使只创建但没有实际执行，也不会被误判为通过。
            record = {
                "model": str(path), "model_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "session_providers": session.get_providers(), "first_run_completed": False,
                "versions": package_versions(),
                "pid": os.getpid(), "node_provider_counts": {},
            }
            session._docqa_probe = (record, prefix.with_suffix(".session.json"), threading.Lock())
            session._docqa_probe[1].write_text(json.dumps(record, indent=2), encoding="utf-8")

    def traced_run(session, *args, **kwargs):
        probe = getattr(session, "_docqa_probe", None)
        if probe is None:
            return original_run(session, *args, **kwargs)
        record, target, lock = probe
        with lock:
            result = original_run(session, *args, **kwargs)
            if not record["first_run_completed"]:
                profile = Path(session.end_profiling())
                record["node_provider_counts"] = summarize_profile(json.loads(profile.read_text(encoding="utf-8")))
                record["profile"] = str(profile)
                record["first_run_completed"] = True
                target.write_text(json.dumps(record, indent=2), encoding="utf-8")
                print("DOCQA_OCR_PROBE " + json.dumps(record), flush=True)
            return result

    ort.InferenceSession.__init__ = traced_init
    ort.InferenceSession.run = traced_run


def main():
    install_probe(Path(os.environ["DOCQA_OCR_PROBE_DIR"]))
    # 加载原本的 console entry point，保持 Docling Serve 的命令行启动路径。
    entry = next(e for e in importlib.metadata.distribution("docling-serve").entry_points
                 if e.group == "console_scripts" and e.name == "docling-serve")
    sys.argv = ["docling-serve", "run"]
    entry.load()()


if __name__ == "__main__":
    main()
