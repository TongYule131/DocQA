"""真实链路验收脚本：通过 HTTP 走完上传 → 异步解析 → 预览 → 来源核对流程。

用途（对应任务书 12.1）：
- 对本机 Docling 解析服务（默认 http://127.0.0.1:5001）执行真实转换；
- 通过本次改造后的 Web API 上传样本、创建解析任务、轮询任务、读取结构化预览；
- 把每份样本的文档 ID、任务 ID、解析版本、耗时、质量告警与来源样例写入证据文件。

约束：
- 只连接本机地址，不读取 .env、不调用 DeepSeek 或 embedding 网关；
- 不修改原件；不把大体积结果写入 Git（输出目录在 data/ 下，已被忽略）。

用法：
    python scripts/acceptance_e2e.py --samples D:\\python\\DocQA-test-files --api http://127.0.0.1:8010
"""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def wait_for_task(client: httpx.Client, task_id: str, timeout: float, log) -> dict:
    """轮询任务直到终态；只读取任务接口，不触发任何收费调用。"""
    started = time.monotonic()
    last = None
    while time.monotonic() - started < timeout:
        task = client.get(f"/api/parse-tasks/{task_id}").json()
        if task != last:
            log(f"  任务阶段：{task['status']} / {task['stage']}"
                + (f" · 上游 {task['upstream_task_id']}" if task.get("upstream_task_id") else ""))
            last = task
        if task["status"] in {"succeeded", "failed", "needs_attention"}:
            return task
        time.sleep(2)
    raise TimeoutError("等待任务超时（本地等待超时，不代表上游一定停止）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=Path(r"D:\python\DocQA-test-files"))
    parser.add_argument("--api", default="http://127.0.0.1:8010")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--output", type=Path, default=Path("data/docling-validation"))
    parser.add_argument("--pattern", default="*")
    parser.add_argument("--only", default="", help="只处理文件名包含该子串的样本")
    args = parser.parse_args()

    if urlsplit(args.api).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("验收脚本只允许连接本机业务服务")
    run_dir = args.output / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-e2e-acceptance")
    run_dir.mkdir(parents=True, exist_ok=False)

    def log(message: str) -> None:
        print(message, flush=True)

    samples = sorted(p for p in args.samples.glob(args.pattern)
                     if p.is_file() and p.suffix.lower() in {".pdf", ".docx", ".xlsx", ".txt"})
    if args.only:
        samples = [p for p in samples if args.only in p.name]
    if not samples:
        parser.error("没有匹配的样本文件")

    rows = []
    # trust_env=False：不继承系统代理，文档只发到本机业务服务。
    with httpx.Client(base_url=args.api, timeout=120, trust_env=False) as client:
        version = client.get("/api/health").json()
        parsing = client.get("/api/parsing/status").json()
        save(run_dir / "service.json", {"health": version, "parsing_status": parsing,
                                        "api": args.api})
        log(f"业务服务 {args.api} 健康；解析服务可达={parsing['reachable']}")
        for sample in samples:
            content = sample.read_bytes()
            row = {"file": sample.name, "sha256": hashlib.sha256(content).hexdigest(),
                   "size": len(content), "started_at": datetime.now(timezone.utc).isoformat()}
            log(f"上传：{sample.name}（{len(content)} 字节）")
            started = time.monotonic()
            upload = client.post("/api/documents",
                                 files={"file": (sample.name, content, None)})
            if upload.status_code != 201:
                row["error"] = f"上传失败 HTTP {upload.status_code}: {upload.text[:300]}"
                rows.append(row)
                save(run_dir / "summary.json", rows)
                log(f"  上传失败：{row['error']}")
                continue
            payload = upload.json()
            document = payload["document"]
            row.update(document_id=document["id"], format=payload["format"],
                       format_source=payload["format_source"], format_note=payload["format_note"],
                       page_count=document.get("page_count"))
            log(f"  识别格式：{payload['format']}（{payload['format_source']}）；{payload['format_note']}")

            submit = client.post(f"/api/documents/{document['id']}/parse",
                                 json={"force": True})
            if submit.status_code not in {200, 202}:
                row["error"] = f"提交解析失败 HTTP {submit.status_code}: {submit.text[:300]}"
                rows.append(row)
                save(run_dir / "summary.json", rows)
                log(f"  提交失败：{row['error']}")
                continue
            task_id = (submit.json().get("task") or {}).get("id")
            row["task_id"] = task_id
            row["submit_status"] = submit.status_code
            log(f"  已创建任务 {task_id}（HTTP {submit.status_code}）")
            try:
                task = wait_for_task(client, task_id, args.timeout, log)
            except TimeoutError as exc:
                row["error"] = str(exc)
                rows.append(row)
                save(run_dir / "summary.json", rows)
                continue
            row["task_status"] = task["status"]
            row["task_stage"] = task["stage"]
            row["task_error"] = task.get("error_message")
            row["upstream_task_id"] = task.get("upstream_task_id")
            row["wall_seconds"] = round(time.monotonic() - started, 2)
            if task["status"] != "succeeded":
                row["error"] = "任务未成功，未继续核对内容"
                rows.append(row)
                save(run_dir / "summary.json", rows)
                log(f"  任务未成功：{task['status']} · {task.get('error_message')}")
                continue

            detail = client.get(f"/api/documents/{document['id']}").json()
            row["parse_version_id"] = detail["active_parse_version_id"]
            row["quality_status"] = detail.get("quality_status")
            content_response = client.get(
                f"/api/documents/{document['id']}/content?limit=1000").json()
            version = content_response["version"]
            row["block_count"] = version["block_count"]
            row["chunk_count"] = version["chunk_count"]
            row["parser_name"] = version["parser_name"]
            row["quality_summary"] = version["quality_summary"]
            row["warnings"] = [{"code": w["code"], "severity": w["severity"],
                                "scope": w["scope"], "page": w.get("page"),
                                "sheet": w.get("sheet_name"), "message": w["message"]}
                               for w in version["warnings"]]
            row["block_types"] = {}
            for block in content_response["blocks"]:
                row["block_types"][block["block_type"]] = row["block_types"].get(block["block_type"], 0) + 1
            # 来源样例：每类来源取前 3 条，用于核对页码/章节/工作表与坐标单位。
            source_samples = []
            for block in content_response["blocks"]:
                for source in block["sources"]:
                    if len(source_samples) >= 3:
                        break
                    source_samples.append({"block_type": block["block_type"],
                                           "text": block["text"][:80], "source": source})
                if len(source_samples) >= 3:
                    break
            row["source_samples"] = source_samples
            chunks = client.get(f"/api/documents/{document['id']}/chunks").json()
            row["chunk_sample"] = chunks[:2]
            # 保存结构化预览，便于人工对照原件（体积受控，仅前 200 块）。
            save(run_dir / f"{sample.stem}.content.json",
                 {"version": version, "blocks": content_response["blocks"][:200]})
            rows.append(row)
            save(run_dir / "summary.json", rows)
            log(f"  完成：版本 {row['parse_version_id']}，块 {row['block_count']}，"
                f"分块 {row['chunk_count']}，质量 {row['quality_status']}，"
                f"告警 {len(row['warnings'])} 条，耗时 {row['wall_seconds']} 秒")
    save(run_dir / "summary.json", rows)
    log(f"证据目录：{run_dir.resolve()}")
    failed = [row for row in rows if row.get("error") or row.get("task_status") != "succeeded"]
    log(f"成功 {len(rows) - len(failed)} / {len(rows)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
