"""指定指标的确定性核对。没有输出证据时保持未确认，不推断原文不存在。"""
from __future__ import annotations

from app.schemas import ExtractionOptions


def _matches(item, metric) -> bool:
    squash = lambda value: "".join((value or "").split())
    return (item.kind == "data" and item.value_text is not None
            and all(squash(getattr(item, key, None)) == squash(value)
                    for key, value in metric.items() if value is not None))


def validate_requested_selection(items, options=None) -> None:
    """关闭补充时拒绝范围外条目；不静默删掉模型输出后发布为成功。"""
    options = normalize_options(options)
    if options["include_highlights"]:
        return
    if any(not any(_matches(item, metric) for metric in options["required_metrics"])
           for item in items):
        from app.analysis_validation import AnalysisOutputError
        raise AnalysisOutputError("unrequested_item", "已关闭重点补充，但返回了不匹配指定指标的条目")


def normalize_options(options=None) -> dict:
    if isinstance(options, ExtractionOptions):
        return options.model_dump()
    return ExtractionOptions.model_validate(options or {}).model_dump()


def metric_coverage(items, options=None) -> dict:
    """每个要求至少对应一个数据项，所有已指定条件必须在同一项精确匹配。

    只忽略空白，不用子串/同义词模糊命中；不从 content 拼凑被省略的字段。
    未指定主体/期间表示该条件不限，不能据此宣称覆盖所有对象和记录。
    """
    options = normalize_options(options)
    checks = []
    for index, metric in enumerate(options["required_metrics"], 1):
        matches = [item.item_id for item in items if _matches(item, metric)]
        checks.append({"metric_id": f"m{index}", **metric,
                       "status": "matched" if matches else "unresolved",
                       "item_ids": matches,
                       "reason": ("已校验数据项的指标名称及指定条件逐字匹配；仍需事实语义复核"
                                  if matches else
                                  "没有匹配全部指定字段的已校验数据项；可能未提取、表述不同或来源不确定，不能判定原文不存在")})
    matched = sum(check["status"] == "matched" for check in checks)
    return {"status": ("not_requested" if not checks else
                       "complete" if matched == len(checks) else "incomplete"),
            "required_count": len(checks), "matched_count": matched, "metrics": checks}
