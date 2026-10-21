"""只读取Markdown结构和字符位置；不执行HTML、脚本、外链或代码围栏。"""
import re


def markdown_spans(text):
    spans=[];start=0;offset=0;fence=None
    for line in text.splitlines(keepends=True):
        marker=re.match(r'^\s{0,3}(`{3,}|~{3,})',line)
        heading=not fence and re.match(r'^\s{0,3}#{1,6}\s+\S',line)
        if heading:
            if text[start:offset].strip():spans.append((start,offset,'text'))
            spans.append((offset,offset+len(line),'section_header'));start=offset+len(line)
        elif not fence and not line.strip():
            if text[start:offset].strip():spans.append((start,offset,'text'))
            start=offset+len(line)
        if marker:
            if fence and marker[1][0]==fence[0] and len(marker[1])>=len(fence):fence=None
            elif not fence:fence=marker[1]
        offset+=len(line)
    if text[start:].strip():spans.append((start,len(text),'text'))
    return spans
