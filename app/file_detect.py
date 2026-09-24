# 上传识别：扩展名与客户端 MIME 只作提示，实际格式由文件内容决定。
#
# 判定顺序（对应任务书 B2）：
#   PDF  → 检查文件标识 %PDF- 与结构（可被 pypdf 打开、未加密、页数在限制内）
#   DOCX/XLSX → 检查 ZIP 包结构与 [Content_Types].xml 中的实际包类型
#   TXT  → 检查 UTF-8 可解码且存在非空内容
# 冲突、损坏、加密与超限都返回可读错误；检查 Office 包时限制解压总量与条目数，
# 不执行宏、外链或嵌入内容（只读 ZIP 目录与少量 XML，不解压运行任何内容）。
import logging
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

# 允许的扩展名到期望格式的映射；.doc/.xls 等旧二进制格式明确不支持。
SUPPORTED_SUFFIXES = {".txt": "txt", ".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}

# 各格式对应的媒体类型；提交给 Docling 时使用清理后的文件名与匹配 MIME。
FORMAT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
}

# Office 包安全检查上限：条目数量与解压后总字节数。
MAX_ZIP_ENTRIES = 2048
MAX_ZIP_UNCOMPRESSED_BYTES = 256 * 1024 * 1024

# 包类型标记：docx 与 xlsx 的主文档部件不同，用于区分实际包类型。
_PACKAGE_MARKERS = {
    "docx": b"wordprocessingml.document.main+xml",
    "xlsx": b"spreadsheetml.sheet.main+xml",
}


class UploadFormatError(Exception):
    """上传识别失败：消息可直接展示给用户。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class DetectedFormat:
    """识别结果：格式、判定依据与补充说明。"""
    format: str
    source: str
    note: str
    page_count: int | None = None


def sanitize_filename(name: str) -> str:
    """清理客户端文件名，仅保留用于显示与提交的安全文件名。

    存储路径由服务端随机 ID 决定；这里去掉目录部分、控制字符与危险字符，
    避免把无后缀的存储路径当作原文件名提交给解析服务。
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip().strip(".")
    if not base:
        base = "document"
    # 保留中英文、数字、点、下划线、连字符与括号，其余替换为下划线。
    base = re.sub(r"[^\w\u4e00-\u9fff.\-()（） ]", "_", base)
    return base[:180] or "document"


def _sniff_prefix(data: bytes) -> str | None:
    """按文件标识快速判断 PDF 与 ZIP（Office）容器。"""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return "zip"
    return None


def _inspect_office_package(data: bytes) -> str:
    """检查 ZIP 包结构与实际包类型，返回 docx 或 xlsx。"""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise UploadFormatError(422, f"Office 文件条目过多（超过 {MAX_ZIP_ENTRIES} 个），已拒绝处理")
            total = sum(info.file_size for info in infos)
            if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise UploadFormatError(
                    422, f"Office 文件解压后体积过大（超过 {MAX_ZIP_UNCOMPRESSED_BYTES // (1024 * 1024)} MB），已拒绝处理")
            names = {info.filename for info in infos}
            if "[Content_Types].xml" not in names:
                raise UploadFormatError(422, "文件是 ZIP 但不含 [Content_Types].xml，不是有效的 DOCX/XLSX")
            # 只读取内容类型清单来判断真实包类型，不解压文档正文。
            try:
                content_types = archive.read("[Content_Types].xml")
            except (KeyError, zipfile.BadZipFile) as exc:
                raise UploadFormatError(422, "无法读取 Office 文件的内容类型清单") from exc
    except zipfile.BadZipFile as exc:
        raise UploadFormatError(422, "Office 文件已损坏或不是有效的 ZIP 包") from exc
    for fmt, marker in _PACKAGE_MARKERS.items():
        if marker in content_types:
            if fmt == "docx" and not any(name.startswith("word/") for name in names):
                raise UploadFormatError(422, "文件声明为 DOCX 但缺少 word/ 部件，可能已损坏")
            if fmt == "xlsx" and not any(name.startswith("xl/") for name in names):
                raise UploadFormatError(422, "文件声明为 XLSX 但缺少 xl/ 部件，可能已损坏")
            return fmt
    raise UploadFormatError(422, "该 Office 文件不是 DOCX 或 XLSX（可能是 PPTX、ODF 或其他 OOXML 类型）")


def _inspect_pdf(data: bytes, max_pages: int) -> int:
    """检查 PDF 结构、加密状态与页数；返回页数。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - pypdf 是必需依赖
        raise UploadFormatError(500, "服务端缺少 PDF 解析依赖") from exc
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise UploadFormatError(422, "暂不支持加密 PDF，请先解密后上传")
        pages = len(reader.pages)
    except UploadFormatError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何结构错误都转换为可读提示
        raise UploadFormatError(422, "PDF 文件已损坏或无法读取，请检查后重新导出") from exc
    if pages <= 0:
        raise UploadFormatError(422, "PDF 不包含任何页面")
    if pages > max_pages:
        raise UploadFormatError(
            422, f"PDF 共 {pages} 页，超过当前解析服务限制 {max_pages} 页；请拆分后再上传")
    return pages


def _inspect_text(data: bytes) -> None:
    """TXT 必须能按 UTF-8 解码且含非空内容。"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadFormatError(422, "TXT 文件需要使用 UTF-8 编码") from exc
    if not text.strip():
        raise UploadFormatError(422, "文件为空或没有可解析的文本")


def detect_format(filename: str, data: bytes, *, max_pdf_pages: int = 100) -> DetectedFormat:
    """根据内容识别格式；扩展名与内容冲突时返回明确错误。"""
    suffix = Path(sanitize_filename(filename)).suffix.lower()
    expected = SUPPORTED_SUFFIXES.get(suffix)
    if expected is None:
        raise UploadFormatError(
            415, "当前仅支持 .pdf、.txt、.docx、.xlsx；.doc、.xls 等旧格式需要先转换")
    if not data:
        raise UploadFormatError(422, "文件为空")

    magic = _sniff_prefix(data)
    if expected == "pdf":
        if magic != "pdf":
            raise UploadFormatError(422, "扩展名为 .pdf，但文件内容不是 PDF（缺少 %PDF- 标识）")
        pages = _inspect_pdf(data, max_pdf_pages)
        return DetectedFormat("pdf", "PDF 文件标识与结构", f"共 {pages} 页", page_count=pages)

    if expected in {"docx", "xlsx"}:
        if magic != "zip":
            raise UploadFormatError(422, f"扩展名为 .{expected}，但文件不是 OOXML（ZIP）容器")
        actual = _inspect_office_package(data)
        if actual != expected:
            raise UploadFormatError(
                422, f"扩展名为 .{expected}，但包内实际类型是 {actual.upper()}；请改用正确的扩展名")
        return DetectedFormat(actual, "OOXML 包结构与内容类型清单", "已校验 ZIP 包类型")

    # TXT：内容必须是 UTF-8 且非空；不接受伪装成文本的二进制文件。
    if magic in {"pdf", "zip"}:
        raise UploadFormatError(422, "扩展名为 .txt，但文件内容不是文本（检测到 PDF 或 ZIP 容器）")
    _inspect_text(data)
    return DetectedFormat("txt", "UTF-8 解码与内容检查", "本地解析，不发送给 Docling")
