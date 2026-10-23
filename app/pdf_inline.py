"""用原PDF文字层验证内联碎片的位置；不凭邻接或题干猜数字属于谁。"""
from collections import Counter
from difflib import SequenceMatcher
import re

from app.schemas import QualityWarning

INLINE_VERSION = 'pdf-inline-verified-v1'
_TYPOGRAPHY = str.maketrans('“”‘’＂＇—–－％０１２３４５６７８９', "''''''---%0123456789")
_FRAGMENT = re.compile(r'[+−-]?\d+(?:[.,]\d+)*(?:\s*[%％])?')


def comparison(value):
    """仅用于对照，保留索引映射；不将归一化后的字符串写回原文。"""
    chars, offsets = [], []
    for i, char in enumerate(value):
        if not char.isspace():
            chars.append(char.translate(_TYPOGRAPHY)); offsets.append(i)
    text = ''.join(chars).rstrip('。.!！?？')
    return text, offsets[:len(text)]


def restore_text(text, pdf_text, fragments):
    """只接受‘原段全文相同，仅缺这些数字’的唯一插入；删改字或额外数字均拒绝。"""
    old, offsets = comparison(text); new, _ = comparison(pdf_text)
    if not old or len(old) > 20000 or len(new) > 22000:
        return None
    edits = []
    for kind, a, b, c, d in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if kind == 'equal': continue
        if kind != 'insert': return None
        edits.append((offsets[a] if a < len(offsets) else len(text.rstrip()), new[c:d]))
    expected = Counter(comparison(f)[0] for f in fragments)
    if not edits or Counter(value for _, value in edits) != expected:
        return None
    # 原段的文字、空格和标点保持原样，仅补入已在原PDF和独立节点双重出现的数字。
    result = text
    for position, value in reversed(edits):
        result = result[:position] + value + result[position:]
    return result


def _box(block):
    if len(block.sources) != 1: return None
    s = block.sources[0]; b = s.bbox
    if not b or s.page is None or (s.coord_origin or b.get('coord_origin')) != 'BOTTOMLEFT':
        return None
    try: return s.page, tuple(float(b[k]) for k in ('l', 'b', 'r', 't'))
    except (KeyError, TypeError, ValueError): return None


def repair_inline_fragments(document, read_box):
    """read_box(page, bbox)只读取原PDF相同坐标，测试可注入许可的合成页面。"""
    blocks = document.blocks; repaired = pending = 0; consumed = set()
    for n, target in enumerate(blocks):
        if target.block_type != 'paragraph' or len(target.text) < 45: continue
        location = _box(target)
        if location is None: continue
        page, bounds = location; left, bottom, right, top = bounds
        candidates = []
        for j in range(max(0, n-6), n):
            block = blocks[j]; value = block.text.strip(); loc = _box(block)
            if (j in consumed or block.block_type != 'paragraph' or len(value) > 24
                    or not _FRAGMENT.fullmatch(value) or loc is None or loc[0] != page): continue
            l, b, r, t = loc[1]
            # 只收同一栏、段内或首行上方6pt以内的数字；页眉、页脚及其他栏不能借入。
            if left <= (l+r)/2 <= right and bottom <= (b+t)/2 <= top+6:
                candidates.append((j, block))
        if not candidates: continue
        pending += len(candidates)
        union_top = max(top, *(_box(b)[1][3] for _, b in candidates))
        pdf_text = read_box(page, (left-1, bottom-1, right+1, union_top+1))
        restored = restore_text(target.text, pdf_text, [b.text.strip() for _, b in candidates])
        if restored is None: continue
        target.text = restored
        target.char_count = len(restored)
        for j, block in candidates:
            # 原碎片及坐标仍留在预览/原始结果中，防止重复进入索引；不把它悄悄删除。
            block.block_type = 'inline_fragment'; consumed.add(j)
            for source in block.sources:
                bound = source.model_copy(deep=True)
                bound.note = '原PDF文字层同坐标核验的内联数字；随所属正文引用'
                target.sources.append(bound)
        repaired += len(candidates)
    return dict(version=INLINE_VERSION, recovered=repaired, unresolved=pending-repaired)


def apply_pdf_inline(document, path):
    """扫描/坏字库只告警，不用文字层替代OCR，也不触发外部解析请求。"""
    import pypdfium2 as pdfium
    try:
        with pdfium.PdfDocument(str(path)) as pdf:
            def read(page, box):
                p = pdf[page-1]; text = p.get_textpage()
                try:
                    l, b, r, t = box
                    return text.get_text_bounded(left=l, bottom=b, right=r, top=t)
                finally:
                    text.close(); p.close()
            result = repair_inline_fragments(document, read)
    except (OSError, ValueError, pdfium.PdfiumError):
        result = dict(version=INLINE_VERSION, recovered=0, unresolved=0, unavailable=True)
    if result['recovered']:
        document.warnings.append(QualityWarning(code='inline_fragments_recovered',severity='info',scope='document',
            message=f"{result['recovered']} 个内联数字已按原PDF文字层与坐标核验归入所属正文；原始节点保留"))
    if result['unresolved']:
        document.warnings.append(QualityWarning(code='inline_fragments_unresolved',severity='warning',scope='document',
            message=f"{result['unresolved']} 个疑似内联数字未能可靠归位；未按邻接顺序猜测数值归属"))
    return result
