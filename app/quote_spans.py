"""原文连续范围选择：模型选服务器编号，服务器回取未改写的连续原文。"""
from __future__ import annotations


QUOTE_SPAN_VERSION = "bounded-quotes-v1"


def quote_spans(text: str) -> list[str]:
    # 不修复OCR或删空格；分段无重叠、无丢字，连接后严格等于原输入。
    text = text.replace("\r\n", "\n")
    spans, start = [], 0
    while start < len(text):
        end = min(len(text), start + 180)
        if end < len(text):
            boundaries = [text.rfind(char, start + 90, end) for char in "。！？；!?;\n"]
            boundary = max(boundaries)
            if boundary >= start + 90:
                end = boundary + 1
        spans.append(text[start:end])
        start = end
    return spans


def task_evidence_payload(entries: list[dict]) -> list[dict]:
    return [{**entry, "content": [{"span": index, "text": text}
             for index, text in enumerate(quote_choices(entry["content"]), 1)]} for entry in entries]


def quote_choices(text: str) -> list[str]:
    """所有候选均为最长600字符的真实连续子串，窗口重叠并覆盖全部输入。"""
    text = text.replace("\r\n", "\n")
    if len(text) <= 600:
        return [text] if text else []
    starts = list(range(0, len(text) - 600, 400)) + [len(text) - 600]
    return [text[start:start + 600] for start in dict.fromkeys(starts)]


def context_segments(text: str) -> list[str]:
    """最多60字符的连续小段；保留OCR空格与标点，连接后仍为原窗口。"""
    text = text.replace('\r\n', '\n')
    result, start = [], 0
    while start < len(text):
        # 文献标号独立成段，避免普通限定小段夹带看似本次引用编号的文本。
        if text[start] == '[' and 0 < text.find(']', start + 1, start + 16) - start < 16:
            end = text.index(']', start + 1) + 1
            result.append(text[start:end])
            start = end
            continue
        end = min(len(text), start + 60)
        bracket = text.find('[', start, end)
        if bracket > start:
            end = bracket
        if end < len(text):
            boundary = max(text.rfind(mark, start + 20, end) for mark in '。；，！？;!?\n|')
            if boundary >= start + 20:
                end = boundary + 1
        result.append(text[start:end])
        start = end
    return result


def task_segment_payload(entries: list[dict]) -> list[dict]:
    # 原窗口只表示一次；不另复制所有限定语，避免为选择器成倍增加输入预算。
    return [{**entry, 'content': [{'span': index, 'segments': {
        str(number): value for number, value in enumerate(context_segments(text), 1)}}
        for index, text in enumerate(quote_choices(entry['content']), 1)]} for entry in entries]
