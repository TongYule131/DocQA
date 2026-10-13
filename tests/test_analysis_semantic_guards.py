"""合成反例验证有限的机械守卫；通过这些测试不代表在线语义验收通过。"""
import json
from types import SimpleNamespace

import pytest

from app.analysis_validation import AnalysisOutputError, validate_extraction_batch


def validate(text, content, limitations):
    payload = {
        "items": [{"kind": "conclusion", "content": content, "refs": [1]}],
        "quotes": {"1": text},
        "sections": {"data": "none", "conclusion": "present", "viewpoint": "none"},
        "limitations": limitations,
    }
    return validate_extraction_batch(
        json.dumps(payload, ensure_ascii=False),
        units=[SimpleNamespace(batch_local_id=1, text=text)], batch_id="b1", max_items=15)


@pytest.mark.parametrize("quoted", ["“2020 未作调整”", "「2020 未作调整」"])
def test_fragment_declared_uninterpretable_cannot_be_extracted(quoted):
    with pytest.raises(AnalysisOutputError) as caught:
        validate("2020 未作调整。以本表为准。", "文档称：2020未作调整。",
                 [f"片段{quoted}缺少上下文，无法完全可靠解释。"])
    assert caught.value.reason == "uncertain_fragment_claimed"


def test_complete_sentence_in_same_unit_remains_usable():
    result = validate("2020 未作调整。以本表为准。", "文档要求以本表为准。",
                      ["片段“2020 未作调整”缺少上下文，无法可靠解释。"])
    assert result.items[0].content == "文档要求以本表为准。"


def test_general_ocr_warning_does_not_reject_supported_fact():
    result = validate("2020 年未作调整。", "文档称2020年未作调整。", ["OCR可能存在错字。"])
    assert result.items


def test_declared_missing_year_character_cannot_be_claimed_as_fact():
    with pytest.raises(AnalysisOutputError) as caught:
        validate("某县2020未作调整。", "某县2020未作调整。",
                 ["ref1 中“2020”后缺少年字，可能存在 OCR 缺字。"])
    assert caught.value.reason == "uncertain_fragment_claimed"


def test_uncertain_year_in_other_reference_does_not_block_good_fact():
    payload = {
        "items": [{"kind": "conclusion", "content": "2020年已完成审计。", "refs": [2]}],
        "quotes": {"2": "2020年已完成审计。"},
        "sections": {"data": "none", "conclusion": "present", "viewpoint": "none"},
        "limitations": ["单元1 中“2020”后缺少年字，可能存在排版缺字。"],
    }
    result = validate_extraction_batch(
        json.dumps(payload, ensure_ascii=False), batch_id="b1", max_items=15,
        units=[SimpleNamespace(batch_local_id=1, text="2020未作调整。"),
               SimpleNamespace(batch_local_id=2, text="2020年已完成审计。")])
    assert result.items


def test_uncertainty_must_refer_to_an_actual_source_fragment():
    result = validate("2020 年未作调整。", "文档称2020年未作调整。",
                      ["“其它栏目残句”无法可靠解释。"])
    assert result.items


def test_reference_number_cannot_be_used_as_absent_table_number():
    with pytest.raises(AnalysisOutputError) as caught:
        validate("表编号：2\n表格内容：金额未标单位", "表2金额未标单位。", ["表3金额未标单位。"])
    assert caught.value.reason == "unknown_table_number"


def test_explicit_source_table_number_is_accepted():
    result = validate("表编号：12\n表格内容：金额未标单位", "表12金额未标单位。", [])
    assert result.items


def test_plain_text_without_table_metadata_is_not_inferred():
    result = validate("表3金额未标单位。", "表3金额未标单位。", [])
    assert result.items
