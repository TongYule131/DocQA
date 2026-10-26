"""只读取Markdown结构和字符位置；不执行HTML、脚本、外链或代码围栏。"""
import re

MARKDOWN_STRUCTURE_VERSION='markdown-structure-v2'


def markdown_row(line):
    """保守识别行内单元格；转义竖线和代码中的竖线不作为分隔符。"""
    value=line.rstrip('\r\n');cells=[];start=0;index=0;ticks=0;separators=[]
    while index<len(value):
        char=value[index]
        if char=='\\':index+=2;continue
        if char=='`':
            end=index+1
            while end<len(value) and value[end]=='`':end+=1
            count=end-index
            if not ticks:ticks=count
            elif count==ticks:ticks=0
            index=end;continue
        if char=='|' and not ticks:separators.append(index)
        index+=1
    if not separators:return None
    for end in separators:cells.append(value[start:end].strip());start=end+1
    cells.append(value[start:].strip())
    if not cells[0]:cells.pop(0)
    if cells and not cells[-1]:cells.pop()
    return cells or None


def markdown_spans(text):
    """标题、代码围栏、管道表格与正文分别保留原始字符范围。"""
    lines=text.splitlines(keepends=True);offsets=[0]
    for line in lines:offsets.append(offsets[-1]+len(line))
    spans=[];start=0;index=0
    def flush(end):
        if text[offsets[start]:offsets[end]].strip():spans.append((offsets[start],offsets[end],'text'))
    while index<len(lines):
        line=lines[index];marker=re.match(r'^ {0,3}(`{3,}|~{3,})',line)
        if marker:
            flush(index);end=index+1
            closing=re.compile(r'^ {0,3}'+re.escape(marker[1][0])+'{'+str(len(marker[1]))+r',}[ \t]*\r?\n?$')
            while end<len(lines) and not closing.fullmatch(lines[end]):end+=1
            if end<len(lines):end+=1
            spans.append((offsets[index],offsets[end],'code'));index=start=end;continue
        if re.match(r'^ {0,3}#{1,6}\s+\S',line):
            flush(index);spans.append((offsets[index],offsets[index+1],'section_header'));index+=1;start=index;continue
        header=markdown_row(line)
        divider=markdown_row(lines[index+1]) if index+1<len(lines) else None
        if header and divider and len(header)==len(divider) and all(re.fullmatch(r':?-{3,}:?',cell) for cell in divider):
            flush(index);end=index+2
            while end<len(lines):
                row=markdown_row(lines[end])
                if not row or len(row)!=len(header):break
                end+=1
            spans.append((offsets[index],offsets[end],'markdown_table'));index=start=end;continue
        if not line.strip():flush(index);index+=1;start=index;continue
        index+=1
    flush(len(lines))
    return spans
