"""独立解析验收：只访问本机 Docling，不读取 .env 或调用在线模型。"""
import argparse
import hashlib
import json
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def save(path, value):
    # 每份结果立即落盘；中断后可以用保存的任务编号继续查询。
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:5001")
    parser.add_argument("--label", default="gpu")
    parser.add_argument("--ocr-lang", default="ch", help="OCR 语言：中文样本默认 ch；该值本身不能唯一确定模型版本")
    parser.add_argument("--pattern", default="*")
    parser.add_argument("--timeout", type=int, default=1900)
    parser.add_argument("--output", type=Path, default=Path("data/docling-validation"))
    args = parser.parse_args()
    # 样本仅发送到本机；禁用代理环境变量，避免意外转发文档。
    if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("验证脚本只允许本机解析服务")
    if args.timeout <= 0 or not args.samples.is_dir():
        parser.error("需要有效样本目录和正数超时时间")
    if not args.ocr_lang.strip():
        parser.error("OCR 语言不能为空")
    if not args.label or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.label):
        parser.error("label 只允许英文、数字、下划线和连字符")
    run = args.output / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + args.label)
    run.mkdir(parents=True, exist_ok=False)
    files = sorted(p for p in args.samples.glob(args.pattern) if p.is_file() and p.suffix.lower() in {".pdf", ".docx", ".xlsx", ".png", ".jpg"})
    if not files:
        parser.error("没有匹配的测试文档")
    rows = []
    with httpx.Client(base_url=args.url, timeout=60, trust_env=False) as client:
        response = client.get("/openapi.json")
        response.raise_for_status()
        save(run / "openapi.json", response.json())
        version = client.get("/version")
        save(run / "version.json", {"http_status": version.status_code, "body": version.text})
        for file in files:
            started = time.monotonic()
            row = {"file": file.name, "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                   "label": args.label, "started_at": datetime.now(timezone.utc).isoformat(),
                   # 记录请求条件；模型身份还需要版本、模型文件哈希等证据。
                   "ocr_lang": args.ocr_lang}
            print(f"开始：{file.name}", flush=True)
            try:
                # 明确指定 RapidOCR；不启用图片描述、公式增强或远程 VLM。
                # Docling 2.128.0 默认也是 ch；显式传入是为固定测试条件，并非修复已证实的语言错误。
                # 上游区分原生代码与 iso: 前缀的语言标签，不应把裸 zh 的失败归因于映射表缺失。
                data = {"to_formats": ["json", "md"], "ocr_engine": "rapidocr", "do_ocr": "true",
                        "ocr_lang": [args.ocr_lang],
                        "force_ocr": "false", "table_mode": "accurate", "image_export_mode": "placeholder"}
                with file.open("rb") as source:
                    response = client.post("/v1/convert/file/async", data=data,
                        files={"files": (file.name, source, mimetypes.guess_type(file.name)[0] or "application/octet-stream")})
                response.raise_for_status()
                task = response.json()
                row["task_id"] = task["task_id"]
                save(run / f"{file.stem}.task.json", task)
                # POST 不自动重试：响应丢失时可能已经创建了任务。
                while task["task_status"] not in {"success", "failure"}:
                    if time.monotonic() - started > args.timeout:
                        raise TimeoutError("等待超时；上游可能仍在执行，请用已保存的 task_id 查询")
                    time.sleep(2)
                    response = client.get(f"/v1/status/poll/{row['task_id']}")
                    response.raise_for_status()
                    task = response.json()
                    save(run / f"{file.stem}.task.json", task)
                row["task_status"] = task["task_status"]
                if task["task_status"] == "failure":
                    # 失败任务不一定存在 result；保留任务原始响应，避免 404 遮盖真正的失败。
                    row["task_details"] = task
                    raise RuntimeError("上游解析任务失败，请检查已保存的 task.json 和服务日志")
                response = client.get(f"/v1/result/{row['task_id']}")
                response.raise_for_status()
                result = response.json()
                save(run / f"{file.stem}.result.json", result)
                row["conversion_status"] = result.get("status")
                row["processing_seconds"] = result.get("processing_time")
                row["errors"] = result.get("errors", [])
                doc = result.get("document") or {}
                markdown = doc.get("md_content") or ""
                (run / f"{file.stem}.md").write_text(markdown, encoding="utf-8")
                structured = doc.get("json_content") or {}
                if isinstance(structured, str):
                    structured = json.loads(structured)
                row.update(markdown_chars=len(markdown), tables=len(structured.get("tables", [])),
                           texts=len(structured.get("texts", [])), pages=len(structured.get("pages", {})))
            except Exception as exc:
                row["client_error"] = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, httpx.HTTPStatusError):
                    # 仅写入本地忽略目录，便于检查接口校验失败的原因。
                    row["response"] = exc.response.text[:4000]
            row["wall_seconds"] = round(time.monotonic() - started, 2)
            rows.append(row)
            save(run / "summary.json", rows)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            # 部分成功也停止，先检查缺失内容；不把未知任务或不完整结果算作整轮通过。
            if "client_error" in row or row.get("task_status") != "success" or row.get("conversion_status") != "success":
                break
    print(f"结果目录：{run.resolve()}", flush=True)
    return 1 if any("client_error" in r or r.get("conversion_status") != "success" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
