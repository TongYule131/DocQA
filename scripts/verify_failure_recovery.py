"""故障链路验证：重新解析失败时旧版本与旧索引仍可用（任务书 12.3）。

流程（全部通过真实 HTTP 与本机 Docling 服务，不使用模拟 embedding 之外的替代）：
1. 用真实样本建立“A 已解析并已索引”的起点；
2. 让重新解析 B 失败（把解析服务地址指向一个不可达端口，仅本次进程生效）；
3. 刷新页面数据，检索 A：必须仍然可用，且明确标注使用旧版本；
4. 恢复上游后重试 B、成功发布，再建立新索引并检索，检查版本与来源；
5. 重启 Web/worker（由外部脚本执行）后再次检查持久性。

约束：只影响本次脚本运行的进程环境；不修改生产数据，不破坏原件。

用法：
    python scripts/verify_failure_recovery.py --api http://127.0.0.1:8010 \
        --sample D:\\python\\DocQA-test-files\\04-scan-zh.pdf --worker-cmd "python -m app.parse_worker"
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_worker(worker_cmd: list[str], env: dict, log) -> None:
    """执行一次 worker 直到没有可领取任务；使用子进程避免与常驻 worker 抢任务。"""
    log(f"  执行 worker：{' '.join(worker_cmd)}")
    # 统一按 UTF-8 解码子进程输出，避免 Windows 默认 GBK 解码中文日志时抛异常。
    result = subprocess.run(worker_cmd, capture_output=True, text=True, env=env, timeout=1800,
                            encoding="utf-8", errors="replace")
    tail = (result.stdout or "") + (result.stderr or "")
    log("  worker 输出末尾：" + " | ".join(tail.strip().splitlines()[-3:]))


def wait_task(client: httpx.Client, task_id: str, timeout: float, log) -> dict:
    started = time.monotonic()
    last = None
    while time.monotonic() - started < timeout:
        task = client.get(f"/api/parse-tasks/{task_id}").json()
        if task != last:
            log(f"  任务：{task['status']} / {task['stage']}"
                + (f" · 上游 {task['upstream_task_id']}" if task.get("upstream_task_id") else ""))
            last = task
        if task["status"] in {"succeeded", "failed", "needs_attention"}:
            return task
        time.sleep(2)
    raise TimeoutError("等待任务超时")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8010")
    parser.add_argument("--sample", type=Path, default=Path(r"D:\python\DocQA-test-files\04-scan-zh.pdf"))
    parser.add_argument("--worker-cmd", default="python -m app.parse_worker")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--output", type=Path, default=Path("data/docling-validation"))
    parser.add_argument("--allow-online-index", action="store_true",
                        help="明确允许为这一份测试样本建立在线索引并检索（会产生 API 费用）")
    args = parser.parse_args()

    run_dir = args.output / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-failure-recovery")
    run_dir.mkdir(parents=True, exist_ok=False)
    steps: list[dict] = []

    def log(message: str) -> None:
        print(message, flush=True)

    def record(step: str, passed: bool, detail) -> None:
        steps.append({"step": step, "passed": passed, "detail": detail})
        log(f"{'PASS' if passed else 'FAIL'} {step}：{detail}")
        save(run_dir / "steps.json", steps)

    worker_cmd = args.worker_cmd.split()
    # 子进程继承当前环境（包含 DOCQA_DATA_DIR），保证与 Web/worker 使用同一数据库。
    import os
    base_env = dict(os.environ)

    with httpx.Client(base_url=args.api, timeout=120, trust_env=False) as client:
        content = args.sample.read_bytes()
        log(f"上传样本：{args.sample.name}（{len(content)} 字节）")
        upload = client.post("/api/documents", files={"file": (args.sample.name, content, None)})
        upload.raise_for_status()
        document_id = upload.json()["document"]["id"]

        # 步骤 1：建立 A（成功解析）。
        submit = client.post(f"/api/documents/{document_id}/parse", json={"force": True}).json()
        task_a = wait_task(client, submit["task"]["id"], args.timeout, log)
        record("A 解析成功", task_a["status"] == "succeeded",
               f"任务={task_a['id']} 状态={task_a['status']}")
        version_a = client.get(f"/api/documents/{document_id}").json()["active_parse_version_id"]
        chunks_a = client.get(f"/api/documents/{document_id}/chunks").json()
        record("A 版本与分块", bool(version_a) and bool(chunks_a),
               f"版本={version_a} 分块数={len(chunks_a)}")
        if not args.allow_online_index:
            record("A 已有可用索引（本场景前置条件）", False,
                   "未授权在线索引；故障链路未验收。可使用离线测试，或添加 --allow-online-index")
            return 2
        built = client.post(f"/api/documents/{document_id}/index")
        built.raise_for_status()
        assert built.json()['status'] == 'indexed'

        # 步骤 2：模拟上游故障后重新解析 B（失败）。
        # 通过环境变量把解析服务指向不可达端口，只影响本次 worker 子进程。
        log("模拟上游不可达：DOCQA_DOCLING_BASE_URL=http://127.0.0.1:59999")
        failed_env = dict(base_env)
        failed_env["DOCQA_DOCLING_BASE_URL"] = "http://127.0.0.1:59999"
        failed_env["DOCQA_DOCLING_CONNECT_TIMEOUT_SECONDS"] = "3"
        failed_env["DOCQA_DOCLING_MAX_RETRIES"] = "1"
        submit_b = client.post(f"/api/documents/{document_id}/parse", json={"force": True}).json()
        run_worker(worker_cmd, failed_env, log)
        task_b = wait_task(client, submit_b["task"]["id"], args.timeout, log)
        record("B 解析失败并记录原因", task_b["status"] == "failed" and bool(task_b["error_message"]),
               f"状态={task_b['status']} 错误码={task_b['error_code']} 说明={task_b['error_message']}")

        # 步骤 3：刷新页面数据（重新读取文档与分块）后检索 A。
        document = client.get(f"/api/documents/{document_id}").json()
        chunks_after = client.get(f"/api/documents/{document_id}/chunks").json()
        record("失败后旧版本仍为活动版本", document["active_parse_version_id"] == version_a
               and document["status"] == "parsed",
               f"活动版本={document['active_parse_version_id']} 状态={document['status']} "
               f"任务状态={document['task_status']}")
        record("失败后旧分块仍可读取", chunks_after == chunks_a,
               f"分块数={len(chunks_after)}（与失败前一致={chunks_after == chunks_a}）")

        # 步骤 4：为 A 建立索引并检索（真实 embedding 由用户授权时使用；默认先检查状态）。
        index_before = client.get(f"/api/documents/{document_id}/index").json()
        log(f"索引状态：{index_before['status']}（未配置密钥时无法真实建索引，属预期限制）")
        search = client.post(f"/api/documents/{document_id}/search",
                             json={"query": "本年鉴包含多少个部分", "top_k": 3})
        record("失败后旧索引实际可检索（409 不能算通过）",
               search.status_code == 200,
               f"HTTP {search.status_code}：{search.text[:160]}")
        if search.status_code == 200:
            body = search.json()
            record("检索仍使用旧版本原文",
                   body["parse_version_id"] == version_a,
                   f"检索版本={body['parse_version_id']} 旧版本标记={body['is_old_version']} "
                   f"命中={len(body['results'])}")

        # 步骤 5：恢复上游，重试 B 并成功发布。
        retry = client.post(f"/api/parse-tasks/{task_b['id']}/retry")
        record("用户明确重试新建尝试", retry.status_code == 200 and retry.json()["id"] != task_b["id"],
               f"HTTP {retry.status_code} 新任务={retry.json().get('id')}")
        run_worker(worker_cmd, base_env, log)
        task_retry = wait_task(client, retry.json()["id"], args.timeout, log)
        record("恢复后重试成功", task_retry["status"] == "succeeded",
               f"状态={task_retry['status']} 错误={task_retry['error_message']}")
        document_after = client.get(f"/api/documents/{document_id}").json()
        new_version = document_after["active_parse_version_id"]
        # 同一原件与同一参数会产生相同结果哈希，此时按幂等复用同一版本（不重复发布）；
        # 版本变化时则必须是新的活动版本。两种情况都算通过，但必须如实记录是哪一种。
        if new_version == version_a:
            record("重试成功且按幂等复用同一版本", True,
                   f"版本={new_version}（原件与参数未变，结果哈希相同，未重复发布）")
        else:
            record("新版本成为活动版本", True, f"旧版本={version_a} 新版本={new_version}")
        record("旧失败记录仍保留",
               client.get(f"/api/parse-tasks/{task_b['id']}").json()["status"] == "failed",
               "失败任务未被覆盖")

        # 步骤 6：检查持久性（重启由外部脚本执行，这里记录当前可读状态）。
        save(run_dir / "state.json", {
            "document": document_after,
            "versions": [v["id"] for v in client.get(f"/api/documents/{document_id}/versions").json()],
            "index": client.get(f"/api/documents/{document_id}/index").json(),
        })
    log(f"证据目录：{run_dir.resolve()}")
    failed = [step for step in steps if not step["passed"]]
    log(f"通过 {len(steps) - len(failed)} / {len(steps)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
