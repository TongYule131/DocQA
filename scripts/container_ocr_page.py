"""在容器内用 RapidOCR 独立重新识别扫描页，核对引文是否真的来自原件。

与接入代码使用同一类 OCR 引擎（Docling + RapidOCR），但使用**独立进程、
独立调用路径**，用于确认引用原文在原件图像中确实存在，而不是生成阶段产生的文字。
"""
import json
import sys

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

pdf_path = sys.argv[1]
page_no = int(sys.argv[2]) if len(sys.argv) > 2 else 2

pipeline = PdfPipelineOptions()
pipeline.do_ocr = True
pipeline.ocr_options = RapidOcrOptions(lang=["chinese"], force_full_page_ocr=True)
converter = DocumentConverter(
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)})
result = converter.convert(pdf_path, page_range=(page_no, page_no))
text = result.document.export_to_text()
print(json.dumps({
    "page": page_no,
    "engine": "rapidocr-chinese",
    "chars": len(text),
    "has_21": "21" in text,
    "has_21ge": "21个部分" in text,
    "excerpt": text[:220],
}, ensure_ascii=False))
