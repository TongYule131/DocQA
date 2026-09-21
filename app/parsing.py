# 文档处理层：读取文本并按页切分，尚未执行 OCR 或向量化。
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader

from app.schemas import Chunk, Page


class ParseError(Exception):
    # 可预期的格式或内容错误，由接口层转换为可读提示。
    pass


def parse_document(path: Path, suffix: str) -> list[Page]:
    # 调用方已将扩展名限制为 .txt / .pdf，文件实际路径由服务端 ID 决定。
    if suffix == ".txt":
        try:
            # utf-8-sig 同时兼容普通 UTF-8 和带 BOM 的文本。
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("TXT 文件需要使用 UTF-8 编码") from exc
        if not text.strip():
            raise ParseError("文档没有可解析的文本")
        return [Page(number=1, text=text)]
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ParseError("暂不支持加密 PDF，请先解密")
        # 仅提取 PDF 文本层，保留原始页码，供后续答案追溯来源。
        pages = [Page(number=i + 1, text=page.extract_text() or "") for i, page in enumerate(reader.pages)]
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("PDF 无法解析，请检查文件是否损坏或格式是否正确") from exc
    if not pages:
        raise ParseError("PDF 不包含页面")
    # 任何一页缺少文本都暂停整份解析，避免静默遗漏扫描页；空白页也会触发。
    if any(not page.text.strip() for page in pages):
        raise ParseError("PDF 存在无文本层页面（可能为扫描页或空白页），需要接入 OCR 后解析")
    return pages


def chunk_pages(document_id: str, pages: list[Page], size: int = 800, overlap: int = 120) -> list[Chunk]:
    # 长度按字符数计算而非模型 token 数；重叠部分用于保留边界上下文。
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("分块长度必须大于 0，重叠长度必须小于分块长度")
    chunks = []
    for page in pages:
        # 每页单独切块，不跨页拼接，使一个分块始终对应一个来源页。
        text = page.text.strip()
        for start in range(0, len(text), size - overlap):
            chunks.append(Chunk(id=uuid4().hex, document_id=document_id, page=page.number, text=text[start:start + size]))
            if start + size >= len(text):
                # 最后一块已覆盖页尾，停止循环以免追加仅含重叠内容的短块。
                break
    return chunks
