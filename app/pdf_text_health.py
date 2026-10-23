"""检测明显损坏的PDF文本映射；不猜字、不修补原文，也不宣称OCR准确率。"""
from collections import defaultdict
from io import BytesIO
import unicodedata

PDF_TEXT_HEALTH_VERSION = 'pdf-text-health-v1'


def text_health(text: str) -> dict:
    visible = sum(not char.isspace() for char in text)
    controls = sum(unicodedata.category(char) == 'Cc' and char not in '\t\r\n' for char in text)
    unmapped = sum(unicodedata.category(char) == 'Co' or char == '\ufffd' for char in text)
    # 普通数学运算符、希腊字母、箭头和中英混排不计入装饰字形。
    dingbats = sum('\u2700' <= char <= '\u27bf' for char in text)
    broken = ((controls >= 16 and controls / max(visible, 1) >= .05)
              or (unmapped >= 16 and unmapped / max(visible, 1) >= .1)
              or (dingbats >= 24 and dingbats / max(visible, 1) >= .18))
    return dict(characters=len(text), controls=controls, unmapped=unmapped,
                dingbats=dingbats, suspicious=broken)


def pdf_ocr_plan(content: bytes, *, max_pages: int = 100) -> dict:
    plan = dict(version=PDF_TEXT_HEALTH_VERSION, force_ocr=False, inspected_pages=0,
                suspect_pages=[], status='not_assessed')
    if not content.startswith(b'%PDF-'):
        return plan
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted or not 0 < len(reader.pages) <= max_pages:
            return plan
        plan['total_pages'] = len(reader.pages)
        for number, page in enumerate(reader.pages, 1):
            health = text_health(page.extract_text() or '')
            plan['inspected_pages'] = number
            if health['suspicious']:
                # 任一页明确异常即在首次提交时整份强制OCR，不追加第二次转换。
                plan.update(force_ocr=True, suspect_pages=[number], status='suspect_text_mapping')
                return plan
        plan['status'] = 'no_obvious_mapping_error'
    except Exception:
        # 预检不能代替正式格式校验，无法提取时仍由既有解析路径处理。
        plan['status'] = 'not_assessed'
    return plan


def garbled_pages(document) -> list[tuple[int, dict]]:
    pages = defaultdict(list)
    for block in document.indexable_blocks():
        for page in {s.page for s in block.sources if s.page is not None}:
            pages[page].append(block.text)
    return [(page, health) for page, texts in sorted(pages.items())
            if (health := text_health('\n'.join(texts)))['suspicious']]
