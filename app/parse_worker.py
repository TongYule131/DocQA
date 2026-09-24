"""持久化解析 worker：从 SQLite 领取任务，调用 Docling，规范化、分块并发布版本。

运行方式：``python -m app.parse_worker``

设计要点（对应任务书工程包 C）：
- SQLite 是任务事实来源；Web 进程只创建任务与查询结果，不执行解析；
- 使用条件更新原子领取，记录执行令牌、租约与心跳；续租与最终写入都校验令牌，
  过期执行者不能覆盖新执行者的结果；
- 重启后 queued 可继续；已有上游 task_id 的任务直接恢复查询与领取，不重新上传；
- 提交结果不确定（POST 已接收但响应丢失、拿到响应后本地未保存即崩溃）时进入
  needs_attention，保留恢复信息，只有用户明确重试才新建尝试，并提示可能重复转换；
- 结果文件先写临时目录再原子改名，最后在短事务中发布数据库引用；
  写文件或提交失败时旧版本与旧索引不变；
- 同一结果重复领取、重复恢复或“结果已落盘、尚未发布”阶段中断，都不会生成重复版本；
- 超时只是本次等待超时，不代表远端一定停止；上游 task_id 始终保留；
- 处理 SIGINT 时留下可恢复状态，轮询与心跳不会阻止进程在合理时间内退出。
"""
import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from threading import Event, Thread
from datetime import datetime, timezone
from pathlib import Path

from app import migrations
from app.chunking import CHUNKING_ALGO, ChunkingConfig, chunk_document
from app.config import Settings
from app.document_normalizer import (
    RESULT_SCHEMA_VERSION,
    apply_formula_warnings,
    inspect_xlsx_formulas,
    merge_warnings,
    normalize_docling_result,
    overall_quality_status,
)
from app.docling_client import (
    ERROR_CANCELLED,
    ERROR_DEADLINE,
    ERROR_RESULT_INVALID,
    ERROR_RESULT_MISSING,
    ERROR_SUBMIT_UNCERTAIN,
    DoclingClient,
    DoclingError,
)
from app.repository import Repository, now_iso
from app.schemas import ParseTask, ParseVersion

logger = logging.getLogger("docqa.worker")

# 上游任务不存在/过期：保留原因，允许用户明确重试。
ERROR_UPSTREAM_LOST = "upstream_task_lost"
# 结构无效：不发布，保留失败证据供诊断。
ERROR_QUALITY_INVALID = "quality_invalid"
# 本地文件或数据库故障。
ERROR_LOCAL_IO = "local_io_error"


class ParseWorker:
    """串行处理解析任务的 worker；可注入客户端便于离线测试。"""

    def __init__(self, settings: Settings, repository: Repository | None = None,
                 client: DoclingClient | None = None, *, worker_id: str | None = None,
                 sleep=time.sleep):
        self.settings = settings
        self.repository = repository or Repository(settings.data_dir / "docqa.db")
        self.client = client or DoclingClient(settings)
        self.worker_id = worker_id or f"{os.getpid()}-{int(time.time())}"
        self.sleep = sleep
        self.stop_requested = False
        self.upload_dir = settings.data_dir / "uploads"
        self.results_dir = settings.data_dir / "parse-results"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def request_stop(self, *_args) -> None:
        """SIGINT/SIGTERM 处理：只设置标记，让当前任务留下可恢复状态。"""
        self.stop_requested = True
        logger.warning("收到停止请求：当前任务将保留可恢复状态后退出")

    def run_forever(self, *, max_tasks: int | None = None) -> int:
        """串行循环领取任务；max_tasks 用于测试与一次性运行。"""
        processed = 0
        self.repository.initialize()
        while not self.stop_requested:
            task = self.repository.claim_next_task(self.worker_id, self.settings.worker_lease_seconds)
            if task is None:
                if max_tasks is not None:
                    break
                self.sleep(self.settings.worker_idle_sleep_seconds)
                continue
            self.process_task(task)
            processed += 1
            if max_tasks is not None and processed >= max_tasks:
                break
        return processed

    def run_task(self, task_id: str) -> bool:
        """显式执行指定任务（测试与手动恢复使用）。"""
        task = self.repository.claim_next_task(self.worker_id, self.settings.worker_lease_seconds,
                                               task_id=task_id)
        if task is None:
            logger.info("任务 %s 当前不可领取（可能已完成或由其他 worker 持有）", task_id)
            return False
        self.process_task(task)
        return True

    # ------------------------------------------------------------------
    # 单任务处理
    # ------------------------------------------------------------------
    def process_task(self, task: ParseTask) -> None:
        """心跳覆盖上传、下载和本地转换，避免长请求期间租约过期。"""
        done = Event()

        def heartbeat():
            while not done.wait(max(0.2, self.settings.worker_lease_seconds / 3)):
                try:
                    if not self.repository.renew_lease(task.id, task.lease_token or "",
                                                       self.settings.worker_lease_seconds):
                        return
                except Exception:
                    logger.exception("任务 %s 续租失败，后续发布仍须通过租约检查", task.id)
                    return

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self._process_task(task)
        finally:
            done.set()
            thread.join(timeout=2)

    def _process_task(self, task: ParseTask) -> None:
        """执行一个任务：提交/恢复 → 等待 → 领取 → 规范化 → 分块 → 发布。"""
        token = task.lease_token or ""
        logger.info("开始处理任务 %s（文档 %s，阶段 %s，上游 %s）",
                    task.id, task.document_id, task.stage, task.upstream_task_id or "无")
        try:
            if self.repository.task_cancelled(task.id):
                self.repository.mark_task_needs_attention(task.id, token, "waiting_paused", "等待已暂停，可明确恢复")
                return
            document = self.repository.get(task.document_id)
            if document is None:
                self.repository.finish_task(task.id, token, status="failed", stage="done",
                                            error_code=ERROR_LOCAL_IO, error_message="文档记录不存在")
                return
            path = self.upload_dir / document.id
            if not path.exists():
                self.repository.finish_task(task.id, token, status="failed", stage="done",
                                            error_code=ERROR_LOCAL_IO,
                                            error_message="原件文件不存在，请重新上传")
                return
            content = path.read_bytes()
            origin_hash = hashlib.sha256(content).hexdigest()
            fmt = document.format or self._format_from_name(document.filename)
            if fmt == "txt":
                self._process_local_text(task, token, document, content, origin_hash)
                return
            upstream_id = task.upstream_task_id
            if upstream_id:
                # 已有上游任务编号：直接恢复查询与领取，绝不重新上传。
                self.repository.update_task_progress(task.id, token, stage="waiting_upstream")
                logger.info("任务 %s 恢复已有上游任务 %s", task.id, upstream_id)
            else:
                upstream_id = self._submit(task, token, document, content, fmt)
                if upstream_id is None:
                    return
            result = self._wait_for_result(task, token, upstream_id)
            if result is None:
                return
            if result.conversion_status != "success" or result.errors:
                raise DoclingError(ERROR_RESULT_INVALID, "解析结果未完整成功，保留原版本")
            self._publish(task, token, document, result.json_content, result.md_content,
                          origin_hash=origin_hash, parser_version=None, source="docling")
        except DoclingError as exc:
            self._handle_docling_error(task, token, exc)
        except Exception:  # noqa: BLE001 - worker 必须把任意异常转换为任务终态
            logger.exception("任务 %s 处理失败", task.id)
            self.repository.finish_task(
                task.id, token, status="failed", stage="done",
                error_code=ERROR_LOCAL_IO,
                error_message="本地处理失败，请检查服务日志后重试；原有解析版本与索引不受影响")

    # ------------------------------------------------------------------
    # 提交与等待
    # ------------------------------------------------------------------
    def _submit(self, task: ParseTask, token: str, document, content: bytes, fmt: str) -> str | None:
        """提交给 Docling；提交结果不确定时不自动重投。"""
        from app.file_detect import FORMAT_MIME, sanitize_filename

        if not self.repository.update_task_progress(task.id, token, stage="submitting"):
            return None
        filename = sanitize_filename(document.filename)
        try:
            payload = self.client.submit(filename=filename, content=content,
                                         mime=FORMAT_MIME.get(fmt, "application/octet-stream"))
        except DoclingError as exc:
            if exc.uncertain:
                # 不确定窗口：上游可能已创建任务。这里不自动重投，交由用户确认。
                self.repository.mark_task_needs_attention(
                    task.id, token, exc.code,
                    exc.message + "（本地幂等键不能保证上游 exactly-once，重复提交可能产生重复转换）")
                return None
            if exc.retryable:
                self.repository.finish_task(task.id, token, status="failed", stage="done",
                                            error_code=exc.code, error_message=exc.message)
                return None
            raise
        upstream_id = payload.get("task_id")
        # 先持久化上游 task_id 再继续轮询：崩溃后可以据此恢复，不重复上传。
        if not self.repository.update_task_progress(task.id, token, stage="waiting_upstream",
                                                   upstream_task_id=upstream_id):
            # 令牌失效（例如进程重启后由新执行者接管）：不继续占用上游任务。
            logger.warning("任务 %s 提交后令牌失效，已放弃本次执行", task.id)
            return None
        logger.info("任务 %s 已提交上游，task_id=%s", task.id, upstream_id)
        return upstream_id

    def _wait_for_result(self, task: ParseTask, token: str, upstream_id: str):
        """轮询等待结果，并在等待期间续租；停止请求会在轮询间隙生效。"""
        last_renew = time.monotonic()

        def should_stop() -> bool:
            return self.stop_requested or self.repository.task_cancelled(task.id)

        def on_poll(status: str) -> None:
            nonlocal last_renew
            if time.monotonic() - last_renew > max(1.0, self.settings.worker_lease_seconds / 3):
                if not self.repository.renew_lease(task.id, token, self.settings.worker_lease_seconds):
                    # 续租失败：本执行者已失效，立即停止以免覆盖新执行者结果。
                    logger.warning("任务 %s 续租失败，本执行者已失效，停止处理", task.id)
                    self.stop_requested = True
                last_renew = time.monotonic()

        try:
            result = self.client.wait_for_result(upstream_id, should_stop=should_stop, on_poll=on_poll)
        except DoclingError as exc:
            if exc.code == ERROR_CANCELLED:
                # 取消：保留上游 task_id，任务回到 queued 以便恢复，不丢进度。
                # 进程正常退出可自动恢复；用户暂停必须明确恢复，防止队列空转。
                paused = self.repository.task_cancelled(task.id)
                self.repository.finish_task(task.id, token,
                                            status="needs_attention" if paused else "queued",
                                            stage="waiting_upstream", error_code="waiting_paused" if paused else None,
                                            error_message="上游任务编号已保留，可恢复继续")
                return None
            raise
        self.repository.update_task_progress(task.id, token, stage="fetching_result")
        return result

    # ------------------------------------------------------------------
    # 本地 TXT 解析（不发送给 Docling）
    # ------------------------------------------------------------------
    def _process_local_text(self, task: ParseTask, token: str, document, content: bytes,
                            origin_hash: str) -> None:
        """TXT 在本地解析：保留旧逻辑页兼容，同时新增行范围来源。"""
        from app.file_detect import UploadFormatError, _inspect_text

        try:
            _inspect_text(content)
        except UploadFormatError as exc:
            self.repository.finish_task(task.id, token, status="failed", stage="done",
                                        error_code=ERROR_RESULT_INVALID, error_message=str(exc))
            return
        text = content.decode("utf-8-sig")
        payload = {
            "schema_name": "local-text",
            "version": "1",
            "body": {"self_ref": "#/body", "children": [{"$ref": "#/texts/0"}]},
            "texts": [{"self_ref": "#/texts/0", "label": "text", "text": text,
                       "parent": {"$ref": "#/body"}, "prov": [{"page_no": 1, "charspan": [0, len(text)]}]}],
            "tables": [], "groups": [], "pictures": [],
            "pages": {"1": {"page_no": 1}},
        }
        self.repository.update_task_progress(task.id, token, stage="normalizing")
        normalized = normalize_docling_result(document.id, payload, "txt",
                                             parser_version="local-text-1")
        # 补充 TXT 的行范围来源，并说明其页码不是物理 PDF 页码。
        lines = text.splitlines()
        for block in normalized.blocks:
            for source in block.sources:
                source.line_start = 1
                source.line_end = max(1, len(lines))
                source.note = "TXT 逻辑页，不是物理 PDF 页码；行范围为全文行号"
        normalized.warnings.append(
            _txt_warning(len(lines)))
        self._publish(task, token, document, payload, text, origin_hash=origin_hash,
                      parser_version="local-text-1", source="local",
                      normalized=normalized)

    # ------------------------------------------------------------------
    # 规范化、分块与发布
    # ------------------------------------------------------------------
    def _publish(self, task: ParseTask, token: str, document, json_content: dict,
                 md_content: str | None, *, origin_hash: str, parser_version: str | None,
                 source: str, normalized=None) -> None:
        """规范化 → 分块 → 写文件 → 短事务发布。"""
        self.repository.update_task_progress(task.id, token, stage="normalizing")
        fmt = document.format or self._format_from_name(document.filename)
        if normalized is None:
            try:
                normalized = normalize_docling_result(document.id, json_content, fmt,
                                                     parser_version=parser_version)
            except ValueError as exc:
                # 结构无效：不发布，保留失败证据（结果文件仍写盘供诊断）。
                self._save_diagnostic(task, json_content, md_content, str(exc))
                self.repository.finish_task(
                    task.id, token, status="failed", stage="done", error_code=ERROR_RESULT_INVALID,
                    error_message=f"解析结果结构无效，未发布新版本：{exc}")
                return
        if fmt == "xlsx":
            formula_info = inspect_xlsx_formulas(self.upload_dir / document.id)
            apply_formula_warnings(normalized, formula_info)
        normalized.warnings = merge_warnings(normalized.warnings)
        quality_status = overall_quality_status(normalized.warnings)
        if quality_status == "invalid":
            reasons = "；".join(w.message for w in normalized.warnings if w.severity == "error")
            self._save_diagnostic(task, json_content, md_content, reasons)
            self.repository.finish_task(
                task.id, token, status="failed", stage="done", error_code=ERROR_QUALITY_INVALID,
                error_message=f"内容结构校验未通过，未发布新版本：{reasons}")
            return

        self.repository.update_task_progress(task.id, token, stage="chunking")
        config = ChunkingConfig.from_settings(self.settings)
        try:
            chunks = chunk_document(normalized, config)
        except ValueError as exc:
            self.repository.finish_task(task.id, token, status="failed", stage="done",
                                        error_code="chunking_invalid", error_message=str(exc))
            return
        if not chunks:
            # 整份文档没有可索引内容：给出明确原因，不创建空的“成功索引”。
            self.repository.finish_task(
                task.id, token, status="failed", stage="done", error_code=ERROR_QUALITY_INVALID,
                error_message="解析成功但整份文档没有可索引内容（可能为空白文档），未发布新版本")
            return

        # 结果文件：先写临时目录，再原子改名；失败时旧版本不变。
        result_hash = hashlib.sha256(json.dumps(
            {"schema": RESULT_SCHEMA_VERSION, "chunking": CHUNKING_ALGO,
             "origin": origin_hash, "quality": quality_status,
             "blocks": [{"t": b.block_type, "x": b.text, "table": b.table, "s": [s.model_dump() for s in b.sources]}
                        for b in normalized.blocks],
             "chunks": [c.text for c in chunks]},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        version_id = f"v-{document.id[:12]}-{result_hash[:16]}"
        # 分块与块绑定解析版本 ID；块 ID 同时带上版本前缀，保证同一文档的
        # 不同解析版本之间块 ID 不冲突（版本内容相同时按结果哈希幂等复用）。
        block_id_map: dict[str, str] = {}
        for block in normalized.blocks:
            block.parse_version_id = version_id
            original_id = block.id
            block.id = f"{version_id}-b{block.order_index}"
            block_id_map[original_id] = block.id
        for chunk in chunks:
            chunk.parse_version_id = version_id
            # 分块必须指向本版本真实存在的块，否则外键校验会失败。
            if chunk.block_id:
                chunk.block_id = block_id_map.get(chunk.block_id, chunk.block_id)
        for warning in normalized.warnings:
            if warning.block_id:
                warning.block_id = block_id_map.get(warning.block_id, warning.block_id)

        # 绑定完成后再落盘，保证规范化文件与数据库引用同一组版本化块编号。
        json_path, md_path = self._write_result_files(version_id, json_content, md_content, normalized)

        self.repository.update_task_progress(task.id, token, stage="publishing")
        version = ParseVersion(
            id=version_id, document_id=document.id, task_id=task.id, origin_hash=origin_hash,
            parser_name=source, parser_version=parser_version,
            config_summary=json.dumps(self.client.config_summary(), ensure_ascii=False),
            result_schema_version=RESULT_SCHEMA_VERSION, result_hash=result_hash, quality_status=quality_status,
            quality_summary=self._quality_summary(normalized.warnings, quality_status),
            block_count=len(normalized.blocks), page_count=normalized.page_count,
            chunk_count=len(chunks), created_at=now_iso(),
            result_json_path=json_path, markdown_path=md_path,
        )
        published, created = self.repository.publish_parse_version(
            task.id, token, version, normalized.blocks, chunks, normalized.warnings)
        if published is None:
            # 令牌失效：本次结果不发布；结果文件保留待清理，不生成重复版本。
            logger.warning("任务 %s 发布时令牌失效，未发布版本 %s", task.id, version_id)
            return
        if not created:
            logger.info("任务 %s 的结果已发布过（%s），本次复用不重复写入", task.id, published)
        self.repository.finish_task(task.id, token, status="succeeded", stage="done")
        logger.info("任务 %s 完成：版本 %s，块 %s，分块 %s，质量 %s",
                    task.id, published, len(normalized.blocks), len(chunks), quality_status)

    def _write_result_files(self, version_id: str, json_content: dict, md_content: str | None,
                            normalized) -> tuple[str, str | None]:
        """把结果写入版本目录：先完整写入临时文件，再逐个原子改名，最后写完成标记。

        为什么不用“临时目录整体改名”：Windows 上 os.replace 不能把目录改名到已存在目录，
        会返回 PermissionError。这里改为逐文件原子替换 + 完成标记：
        - 进程在写入中途崩溃时不会留下“看似完整”的结果目录（没有完成标记）；
        - 重试时按完成标记判断，存在则复用，不存在则整体重写；
        - 任何写入失败都不影响已发布版本，也不改变活动版本指针。
        """
        target_dir = self.results_dir / version_id
        json_rel = f"parse-results/{version_id}/document.json"
        md_rel = f"parse-results/{version_id}/document.md" if md_content is not None else None
        marker = target_dir / "completed.json"
        if marker.exists():
            return json_rel, md_rel
        self.results_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix=f".{version_id}-", dir=self.results_dir))
        try:
            (tmp_dir / "document.json").write_text(
                json.dumps(json_content, ensure_ascii=False), encoding="utf-8")
            (tmp_dir / "normalized.json").write_text(
                json.dumps({
                    "schema": RESULT_SCHEMA_VERSION,
                    "stats": normalized.stats,
                    "quality_status": overall_quality_status(normalized.warnings),
                    "warnings": [w.model_dump() for w in normalized.warnings],
                    "blocks": [b.model_dump() for b in normalized.blocks],
                }, ensure_ascii=False, indent=1), encoding="utf-8")
            if md_content is not None:
                (tmp_dir / "document.md").write_text(md_content, encoding="utf-8")
            (tmp_dir / "completed.json").write_text(
                json.dumps({"version_id": version_id, "schema": RESULT_SCHEMA_VERSION,
                            "written_at": now_iso()}, ensure_ascii=False), encoding="utf-8")
            target_dir.mkdir(parents=True, exist_ok=True)
            for item in sorted(tmp_dir.iterdir()):
                os.replace(item, target_dir / item.name)
            # 清理可能存在的旧临时目录（上次中断留下的），不影响已发布版本。
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        return json_rel, md_rel

    def _save_diagnostic(self, task: ParseTask, json_content: dict, md_content: str | None,
                         reason: str) -> None:
        """保存失败证据：不发布版本，但保留结果供诊断（目录在 data/ 下，已被 Git 忽略）。"""
        try:
            diag_dir = self.results_dir / f"failed-{task.id}"
            diag_dir.mkdir(parents=True, exist_ok=True)
            (diag_dir / "document.json").write_text(
                json.dumps(json_content, ensure_ascii=False), encoding="utf-8")
            if md_content is not None:
                (diag_dir / "document.md").write_text(md_content, encoding="utf-8")
            (diag_dir / "reason.txt").write_text(reason, encoding="utf-8")
        except OSError:
            logger.warning("保存失败证据时发生本地写入错误：任务 %s", task.id)

    @staticmethod
    def _quality_summary(warnings, quality_status: str) -> str:
        if quality_status == "ok":
            return "结构可用，未发现需要提示的质量问题"
        codes = sorted({w.code for w in warnings})
        errors = [w for w in warnings if w.severity != "info"]
        if not errors:
            return "结构可用，仅有提示级信息（如空白页、图片未语义解析、OCR 局限）：" + "、".join(codes)
        return "结构可用但有告警：" + "、".join(codes)

    @staticmethod
    def _format_from_name(filename: str) -> str:
        suffix = Path(filename).suffix.lower().lstrip(".")
        return suffix if suffix in {"txt", "pdf", "docx", "xlsx"} else "pdf"

    def _handle_docling_error(self, task: ParseTask, token: str, exc: DoclingError) -> None:
        """把客户端错误映射为任务终态；不泄露上游原始响应。"""
        if exc.code == ERROR_SUBMIT_UNCERTAIN:
            self.repository.mark_task_needs_attention(task.id, token, exc.code, exc.message)
            return
        if exc.code == ERROR_CANCELLED:
            self.repository.finish_task(task.id, token, status="queued", stage="waiting_upstream",
                                        error_message="已请求停止，可恢复继续")
            return
        if exc.code == ERROR_RESULT_MISSING:
            # 上游任务丢失/过期（查询或领取结果返回 404）：记录原因，允许用户明确重试。
            # 容器重启不保证任务保留，模型缓存存在也不等于任务存在。
            self.repository.finish_task(
                task.id, token, status="failed", stage="done", error_code=ERROR_UPSTREAM_LOST,
                error_message=exc.message + "；容器重启不保证任务保留，可重新解析")
            return
        self.repository.finish_task(task.id, token, status="failed", stage="done",
                                    error_code=exc.code, error_message=exc.message)


def _txt_warning(line_count: int):
    from app.schemas import QualityWarning
    return QualityWarning(
        code="txt_logical_page", severity="info", scope="document",
        message=f"TXT 共 {line_count} 行，按单个逻辑页处理；其页码不是物理 PDF 页码",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DocQA 解析 worker：从 SQLite 领取并执行解析任务")
    parser.add_argument("--once", action="store_true", help="只处理当前可领取的任务后退出")
    parser.add_argument("--task", help="只执行指定任务 ID（用于手动恢复）")
    parser.add_argument("--max-tasks", type=int, default=None, help="最多处理多少个任务后退出")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    worker = ParseWorker(settings)
    signal.signal(signal.SIGINT, worker.request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, worker.request_stop)
    if args.task:
        worker.repository.initialize()
        worker.run_task(args.task)
        return 0
    max_tasks = 1 if args.once else args.max_tasks
    processed = worker.run_forever(max_tasks=max_tasks)
    logger.info("worker 退出，本次处理 %s 个任务", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
