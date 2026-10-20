"""原文限定线索索引：只提取连续语句，不判定其适用事实，不生成新事实。"""
from __future__ import annotations

import re

QUALIFIER_CUE_VERSION = "literal-qualifier-cues-v1"
_MARKERS = re.compile(r"未经|审计|仅|只|不|未|没有|缺失|除|口径|预算|分母|重复|最多|至少|仍|本行|本节|范围|须|应当|应在|应按|应以|应由|且|方可|同时|但|限定|无法")
_SCOPE_MARKERS = re.compile(r"每项|全表|统一|本行|本节|仅|口径|分母|范围|除|但|仍")


def qualifier_cues(text: str) -> list[dict]:
    """最多6条完整短语句，原文偏移可核对；较长语句跳过，不截断例外。"""
    text = text.replace("\r\n", "\n")
    candidates = []
    for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text):
        raw = match.group()
        value = raw.strip()
        # “单元格范围／行范围”是定位包装，不能仅凭这个词成为条件线索。
        search_view = re.sub(r"单元格范围：[^；\n]*|行范围：第 \d+(?:-\d+)? 行（表内行号，含表头 \d+ 行）", "", value)
        if not 3 <= len(value) <= 240 or not _MARKERS.search(search_view):
            continue
        start = match.start() + len(raw) - len(raw.lstrip())
        candidates.append((len(_SCOPE_MARKERS.findall(value)), dict(start=start, end=start + len(value), text=value)))
    candidates.sort(key=lambda item: (-item[0], item[1]["start"]))
    return [item[1] for item in candidates[:6]]


def cue_payload(cues: list[dict]) -> dict:
    # 不向模型提供新增数字编号，避免把字符偏移误作业务数据。
    return {"scope_verified": False, "texts": [cue["text"] for cue in cues]}
