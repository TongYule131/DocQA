"""为损坏文本映射生成仅供本地OCR提交的图像PDF；不替换上传原件。"""
from io import BytesIO
from threading import Lock
import math

from pypdf import PdfReader, PdfWriter

RASTER_VERSION = 'pdf-raster-ocr-v1'
RASTER_DPI = 144
_pdfium_lock = Lock()  # PDFium不允许同一进程内并行调用，包括不同文档。


class PdfRasterError(Exception):
    pass


def rasterize_pdf(content: bytes, *, max_pages: int, max_bytes: int,
                  max_page_pixels: int = 25_000_000) -> bytes:
    import pypdfium2 as pdfium
    with _pdfium_lock, pdfium.PdfDocument(content) as document, PdfWriter() as writer:
        if not 0 < len(document) <= max_pages:
            raise PdfRasterError('异常文本PDF页数超过OCR转换限制，原版本保留')
        estimated = 0
        for page in document:
            try:
                width, height = page.get_size()
                if (not all(math.isfinite(v) and v > 0 for v in (width,height))
                        or math.ceil(width*2)*math.ceil(height*2) > max_page_pixels):
                    raise PdfRasterError('异常文本PDF单页尺寸超过安全OCR渲染限制，原版本保留')
                bitmap = page.render(scale=2)
                try:
                    image = bitmap.to_pil().convert('RGB')
                    buffer = BytesIO()
                    try:
                        image.save(buffer, format='PDF', resolution=RASTER_DPI, quality=95)
                    finally:
                        image.close()
                    estimated += buffer.tell()
                    if estimated > max_bytes:
                        raise PdfRasterError('异常文本PDF的OCR副本超过提交大小限制，原版本保留')
                    reader = PdfReader(buffer)
                    writer.add_page(reader.pages[0])
                    # 保留页面物理尺寸和顺序，OCR坐标仍对应原件同一可视页面。
                    writer.pages[-1].mediabox.upper_right = (width,height)
                finally:
                    bitmap.close()
            finally:
                page.close()
        result = BytesIO()
        writer.write(result)
        if result.tell() > max_bytes:
            raise PdfRasterError('异常文本PDF的OCR副本超过提交大小限制，原版本保留')
        return result.getvalue()
