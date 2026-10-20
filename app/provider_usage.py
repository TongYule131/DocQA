"""从供应商响应提取有限用量元数据；不保存正文、思考或响应头。"""
from __future__ import annotations


def response_metadata(response, *, finish_reason: str | None = None) -> dict:
    usage = getattr(response, "usage", None)
    counts = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, field, None)
        if type(value) is int and value >= 0:
            counts[field] = value
    for field, container, attribute in (
        ("reasoning_tokens", "completion_tokens_details", "reasoning_tokens"),
        ("cached_tokens", "prompt_tokens_details", "cached_tokens"),
    ):
        value = getattr(getattr(usage, container, None), attribute, None)
        if type(value) is int and value >= 0:
            counts[field] = value
    result = {"usage": counts or None, "usage_status": "reported" if counts else "unknown"}
    request_id = getattr(response, "_request_id", None) or getattr(response, "id", None)
    if isinstance(request_id, str) and len(request_id) <= 200 and all(
            char.isalnum() or char in "-_.:" for char in request_id):
        result["provider_request_id"] = request_id
    if finish_reason in {"stop", "length", "content_filter", "tool_calls", "function_call"}:
        result["finish_reason"] = finish_reason
    return result
