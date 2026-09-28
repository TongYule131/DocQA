"""不依赖 Pillow，直接检查扫描 PDF 页面的图像对象与文本层，用于核对引用原文来源。

背景：扫描 PDF 没有文本层，引用原文来自 OCR。本脚本用于确认
1) 该页确实只有图像对象而没有可选文本；
2) 页面图像对象的关键参数（宽高、滤镜、颜色空间），便于记录证据来源。
"""
import re
import sys
from pathlib import Path

from pypdf import PdfReader

pdf_path = Path(sys.argv[1] if len(sys.argv) > 1 else "D:/python/DocQA-test-files/04-scan-zh.pdf")
page_number = int(sys.argv[2]) if len(sys.argv) > 2 else 2

reader = PdfReader(str(pdf_path))
page = reader.pages[page_number - 1]
resources = page.get("/Resources", {})
xobjects = resources.get("/XObject", {}) if resources else {}
print(f"文件：{pdf_path.name}，第 {page_number} 页，共 {len(reader.pages)} 页")
print(f"图像对象数量：{len(xobjects)}")
for name, ref in xobjects.items():
    obj = ref.get_object()
    if obj.get("/Subtype") != "/Image":
        continue
    print(f"  {name}: 宽={obj.get('/Width')} 高={obj.get('/Height')} "
          f"滤镜={obj.get('/Filter')} 颜色空间={obj.get('/ColorSpace')} "
          f"位深={obj.get('/BitsPerComponent')}")
# 文本层：扫描件应当没有可提取文本（这也是必须 OCR 的原因）。
raw = page.extract_text() or ""
print(f"文本层字符数：{len(raw.strip())}")
content = page.get_contents()
data = content.get_data() if content is not None else b""
operators = set(re.findall(rb"\b(TJ|Tj|BT|ET)\b", data))
print(f"内容流中的文本算子：{sorted(op.decode() for op in operators) or '无'}")
