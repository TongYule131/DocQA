"""区分通用准确性提示、实际无正文页与图表未解析，避免误报OCR失败。"""
from app.document_normalizer import normalize_docling_result
from tests.test_parse_pipeline import docling_payload


def test_successfully_extracted_pdf_notice_does_not_claim_ocr_failure():
    normalized=normalize_docling_result("doc",docling_payload(page_count=2),"pdf")
    notice=next(w for w in normalized.warnings if w.code=="ocr_limitation")
    assert notice.severity=="info" and "不表示检测到OCR失败" in notice.message
    assert normalized.indexable_blocks()
    assert not any(w.code=="page_without_indexable_text" for w in normalized.warnings)


def test_picture_only_page_has_located_missing_text_warning_and_image_boundary():
    payload=docling_payload(page_count=3)
    payload["pictures"]=[{"self_ref":"#/pictures/0","label":"picture","parent":{"$ref":"#/body"},
                         "prov":[{"page_no":3}]}]
    payload["body"]["children"].append({"$ref":"#/pictures/0"})
    normalized=normalize_docling_result("doc",payload,"pdf")
    warnings=[w for w in normalized.warnings if w.code=="page_without_indexable_text"]
    assert [w.page for w in warnings]==[3]
    assert warnings[0].severity=="warning"
    assert any(w.code=="pictures_not_parsed" for w in normalized.warnings)
    assert not any(w.code=="blank_page" and w.page==3 for w in normalized.warnings)
