"""Docling 解析服务客户端：提交任务、查询状态、领取结果。

只负责与上游 HTTP 交互，不修改任何业务表（对应任务书 B1）。
固定接口（以当前固定版本 OpenAPI 与已验证脚本为准）：
    POST /v1/convert/file/async
    GET  /v1/status/poll/{task_id}
    GET  /v1/result/{task_id}

错误边界：
- 连接超时、单次读取超时、整体等待期限分别配置，都不能无限阻塞；
- POST 不自动重试（响应丢失时上游可能已经创建任务，盲目重投会产生重复转换）；
- 查询与领取结果可以有限重试并退避，且受整体期限约束；
- 网络错误、HTTP 错误、上游任务失败、结果过期/不存在、结果结构错误分别映射为稳定错误码。
"""
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# 稳定错误码：供任务表 error_code 与页面提示使用，不含上游原始响应内容。
ERROR_CONNECT = "docling_connect_failed"
ERROR_READ_TIMEOUT = "docling_read_timeout"
ERROR_DEADLINE = "docling_deadline_exceeded"
ERROR_HTTP = "docling_http_error"
ERROR_TASK_FAILED = "docling_task_failed"
ERROR_RESULT_MISSING = "docling_result_missing"
ERROR_RESULT_INVALID = "docling_result_invalid"
ERROR_SUBMIT_UNCERTAIN = "docling_submit_uncertain"
ERROR_CANCELLED = "cancelled_by_request"


class DoclingError(Exception):
    """客户端错误：code 为稳定错误码，message 为可展示的安全中文说明。

    uncertain 为 True 表示“上游可能已接收请求但本地无法确认”，调用方必须进入
    needs_attention 而不是自动重投。
    """

    def __init__(self, code: str, message: str, *, uncertain: bool = False,
                 status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain
        self.status_code = status_code
        self.retryable = retryable


@dataclass
class ConversionResult:
    """转换结果：保留任务与转换两个状态，分别判断。"""
    task_id: str
    task_status: str
    conversion_status: str
    errors: list[str] = field(default_factory=list)
    json_content: dict[str, Any] | None = None
    md_content: str | None = None
    processing_time: float | None = None
    raw_task: dict[str, Any] = field(default_factory=dict)


class DoclingClient:
    """按配置与注入构造的 Docling 客户端；测试可注入假上游。"""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        # trust_env=False 禁用环境代理，follow_redirects=False 拒绝任意重定向，
        # 避免文档被转发到配置以外的地址。
        self._client = httpx.Client(
            base_url=settings.docling_base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=settings.docling_connect_timeout_seconds,
                read=settings.docling_read_timeout_seconds,
                write=settings.docling_read_timeout_seconds,
                pool=settings.docling_connect_timeout_seconds,
            ),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DoclingClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 请求参数
    # ------------------------------------------------------------------
    def conversion_form(self, ocr_lang: str | None = None) -> dict[str, Any]:
        """构造转换参数：沿用验收条件，不启用远程 VLM、图片描述或公式增强。"""
        s = self.settings
        return {
            "to_formats": ["json", "md"],
            "ocr_engine": s.docling_ocr_engine,
            "do_ocr": "true",
            "ocr_lang": [ocr_lang or s.docling_ocr_lang],
            "force_ocr": "false",
            "table_mode": s.docling_table_mode,
            "image_export_mode": s.docling_image_export_mode,
        }

    def config_summary(self, ocr_lang: str | None = None) -> dict[str, Any]:
        """配置摘要：写入解析版本，便于判断结果由哪组参数产生。"""
        s = self.settings
        return {
            "ocr_engine": s.docling_ocr_engine,
            "ocr_lang": ocr_lang or s.docling_ocr_lang,
            "do_ocr": True,
            "force_ocr": False,
            "table_mode": s.docling_table_mode,
            "image_export_mode": s.docling_image_export_mode,
            "to_formats": ["json", "md"],
        }

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------
    def submit(self, *, filename: str, content: bytes, mime: str) -> dict[str, Any]:
        """提交转换任务；不自动重试。

        返回上游任务对象（含 task_id）。网络层异常分为两类：
        - 连接失败/连接超时：请求很可能未到达上游，可以安全重试；
        - 写入后读取超时或其他读取错误：上游可能已接收请求，标记 uncertain。
        """
        try:
            response = self._client.post(
                "/v1/convert/file/async",
                data=self.conversion_form(),
                files={"files": (filename, content, mime)},
            )
        except httpx.ConnectError as exc:
            raise DoclingError(ERROR_CONNECT, "无法连接 Docling 解析服务，请确认服务已启动", retryable=True) from exc
        except httpx.ConnectTimeout as exc:
            raise DoclingError(ERROR_CONNECT, "连接 Docling 解析服务超时，请确认服务状态", retryable=True) from exc
        except httpx.ReadTimeout as exc:
            # 提交阶段读取超时：无法确认上游是否已创建任务，必须按不确定处理。
            raise DoclingError(ERROR_SUBMIT_UNCERTAIN,
                               "提交解析请求后等待响应超时，无法确认上游是否已开始转换；"
                               "请先在解析服务中确认任务，再决定是否重试", uncertain=True) from exc
        except httpx.HTTPError as exc:
            raise DoclingError(ERROR_SUBMIT_UNCERTAIN,
                               "提交解析请求时发生网络错误，无法确认上游状态", uncertain=True) from exc
        if response.status_code >= 400:
            raise DoclingError(ERROR_HTTP,
                               self._http_message("提交解析请求", response.status_code),
                               status_code=response.status_code)
        payload = self._json_or_error(response, "提交解析请求")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise DoclingError(ERROR_RESULT_INVALID, "解析服务未返回有效任务编号")
        return payload

    # ------------------------------------------------------------------
    # 查询与领取
    # ------------------------------------------------------------------
    def poll(self, task_id: str) -> dict[str, Any]:
        """查询任务状态；可有限重试并退避。"""
        return self._get_with_retry(f"/v1/status/poll/{task_id}", "查询解析任务状态")

    def fetch_result(self, task_id: str) -> dict[str, Any]:
        """领取结果；结果不存在或过期返回稳定错误码，不重试无意义请求。"""
        try:
            response = self._client.get(f"/v1/result/{task_id}")
        except httpx.TimeoutException as exc:
            raise DoclingError(ERROR_READ_TIMEOUT, "领取解析结果超时，可稍后重试", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise DoclingError(ERROR_CONNECT, "领取解析结果时网络错误", retryable=True) from exc
        if response.status_code == 404:
            raise DoclingError(ERROR_RESULT_MISSING,
                               "解析服务中没有该任务的结果（可能已过期或服务重启后任务被清除）")
        if response.status_code >= 400:
            raise DoclingError(ERROR_HTTP, self._http_message("领取解析结果", response.status_code),
                               status_code=response.status_code)
        return self._json_or_error(response, "领取解析结果")

    def wait_for_result(self, task_id: str, *, should_stop=None, on_poll=None) -> ConversionResult:
        """轮询直到任务终态或整体期限结束，再领取并校验结果。

        - should_stop() 返回 True 时按取消处理，保留可恢复状态（上游 task_id 仍在库中）；
        - on_poll(status) 用于向上层汇报阶段，不表示完成百分比。
        """
        s = self.settings
        deadline = self._clock() + s.docling_total_timeout_seconds
        while True:
            if should_stop is not None and should_stop():
                raise DoclingError(ERROR_CANCELLED, "已请求停止本次等待，上游任务编号已保留，可稍后恢复")
            if self._clock() > deadline:
                # 超时只是本次等待超时，不代表远端一定停止；上游 task_id 必须保留。
                raise DoclingError(ERROR_DEADLINE,
                                   "等待解析结果超过配置的整体期限；上游任务可能仍在执行，"
                                   "已保留任务编号，不会自动新建重复任务")
            task = self.poll(task_id)
            task_status = str(task.get("task_status") or "").lower()
            if on_poll is not None:
                on_poll(task_status)
            if task_status == "success":
                break
            if task_status in {"failure", "failed"}:
                raise DoclingError(ERROR_TASK_FAILED, self._task_failure_message(task))
            if task_status not in {"pending", "started", "running", "queued", "in_progress", ""}:
                # 未知状态不猜测为成功；保留原始状态供诊断。
                logger.warning("Docling 返回未知任务状态：%s", task_status)
            self._sleep(min(s.docling_poll_interval_seconds,
                            max(0.05, deadline - self._clock())))
        return self._build_result(task_id, task)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _get_with_retry(self, path: str, action: str) -> dict[str, Any]:
        """带有限重试与退避的 GET；整体期限由调用方控制。"""
        s = self.settings
        last_error: DoclingError | None = None
        for attempt in range(s.docling_max_retries + 1):
            try:
                response = self._client.get(path)
            except httpx.TimeoutException as exc:
                last_error = DoclingError(ERROR_READ_TIMEOUT, f"{action}超时", retryable=True)
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = DoclingError(ERROR_CONNECT, f"{action}时网络错误", retryable=True)
                last_error.__cause__ = exc
            else:
                if response.status_code == 404:
                    raise DoclingError(ERROR_RESULT_MISSING,
                                       "解析服务中不存在该任务（可能已过期或服务重启后任务被清除）")
                if response.status_code >= 500:
                    last_error = DoclingError(ERROR_HTTP, self._http_message(action, response.status_code),
                                              status_code=response.status_code, retryable=True)
                elif response.status_code >= 400:
                    raise DoclingError(ERROR_HTTP, self._http_message(action, response.status_code),
                                       status_code=response.status_code)
                else:
                    return self._json_or_error(response, action)
            if attempt < s.docling_max_retries:
                self._sleep(s.docling_retry_backoff_seconds * (attempt + 1))
        raise last_error or DoclingError(ERROR_HTTP, f"{action}失败")

    def _build_result(self, task_id: str, task: dict[str, Any]) -> ConversionResult:
        """领取结果并区分任务状态与转换状态。"""
        try:
            payload = self.fetch_result(task_id)
        except DoclingError as exc:
            if exc.code == ERROR_RESULT_MISSING:
                # 任务成功但没有结果：可能是结果已过期，保留任务信息供诊断。
                raise DoclingError(ERROR_RESULT_MISSING,
                                   "任务已完成，但解析服务未提供结果文件（可能已过期）") from exc
            raise
        status = payload.get("status")
        conversion_status = str(status or "").lower()
        errors = payload.get("errors") or []
        if not isinstance(errors, list):
            errors = [str(errors)]
        if conversion_status in {"failure", "failed"}:
            raise DoclingError(ERROR_TASK_FAILED, "解析服务报告转换失败，未发布任何结果")
        if conversion_status != "success" or errors:
            raise DoclingError(ERROR_RESULT_INVALID,
                               "解析结果未完整成功（部分成功、未知状态或含错误），未发布新版本")
        document = payload.get("document")
        if not isinstance(document, dict):
            raise DoclingError(ERROR_RESULT_INVALID, "解析结果缺少 document 字段，结构无效")
        json_content = document.get("json_content")
        if isinstance(json_content, str):
            # 兼容字符串形式的 json_content（实测为对象，此处覆盖字符串兼容测试）。
            try:
                json_content = json.loads(json_content)
            except (TypeError, ValueError) as exc:
                raise DoclingError(ERROR_RESULT_INVALID, "解析结果的 JSON 内容无法解析") from exc
        if not isinstance(json_content, dict):
            raise DoclingError(ERROR_RESULT_INVALID, "解析结果缺少结构化 JSON 内容，无法用于分块与来源定位")
        md_content = document.get("md_content")
        if md_content is not None and not isinstance(md_content, str):
            raise DoclingError(ERROR_RESULT_INVALID, "解析结果的 Markdown 内容格式无效")
        processing = payload.get("processing_time")
        return ConversionResult(
            task_id=task_id,
            task_status="success",
            conversion_status=conversion_status or "success",
            errors=[str(item) for item in errors],
            json_content=json_content,
            md_content=md_content,
            processing_time=processing if isinstance(processing, (int, float)) else None,
            raw_task=task,
        )

    @staticmethod
    def _task_failure_message(task: dict[str, Any]) -> str:
        """只返回稳定提示；截短上游正文不能去除其中的凭证、文件路径或内部堆栈。"""
        return "解析服务报告任务失败；详细原因请查看解析服务日志"

    @staticmethod
    def _http_message(action: str, status_code: int) -> str:
        """HTTP 状态映射为稳定提示；不返回上游响应正文。"""
        mapping = {
            400: "请求参数被解析服务拒绝",
            401: "解析服务要求身份认证（当前客户端未配置凭据）",
            403: "解析服务拒绝访问",
            404: "解析服务接口不存在，请确认服务版本",
            413: "文件超过解析服务允许的大小",
            415: "解析服务不支持该文件类型",
            422: "解析服务无法处理该文件（可能加密、损坏或页数超限）",
            429: "解析服务请求频率受限，请稍后重试",
            500: "解析服务内部错误",
            502: "解析服务网关错误",
            503: "解析服务暂不可用（可能仍在加载模型）",
            504: "解析服务处理超时",
        }
        return f"{action}失败（HTTP {status_code}）：{mapping.get(status_code, '解析服务返回错误')}"

    def _json_or_error(self, response: httpx.Response, action: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise DoclingError(ERROR_RESULT_INVALID, f"{action}返回了无法解析的响应内容") from exc
        if not isinstance(payload, dict):
            raise DoclingError(ERROR_RESULT_INVALID, f"{action}返回的响应不是 JSON 对象")
        return payload

    # ------------------------------------------------------------------
    # 健康检查（不调用模型，不代表模型已预热）
    # ------------------------------------------------------------------
    def probe(self) -> dict[str, Any]:
        """探测解析服务可达性；成功也不表示模型已加载或内容质量合格。"""
        started = time.monotonic()
        try:
            response = self._client.get("/health")
            reachable = response.status_code < 500
            status_code = response.status_code
        except httpx.HTTPError as exc:
            return {"reachable": False, "status_code": None, "detail": "无法连接解析服务",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "error_code": ERROR_CONNECT}
        return {"reachable": reachable, "status_code": status_code,
                "detail": "解析服务可响应（不代表模型已预热或内容质量合格）",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error_code": None if reachable else ERROR_HTTP}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
