"""新任务协议的部分回答与动态上限；旧同步协议保持严格原样。"""
import json

import pytest

from app.rag_prompts import TASK_PROMPT_VERSION, build_task_system_prompt
from app.rag_validation import ModelOutputError, render_markdown, validate_model_output, validate_user_condition_boundary
from test_rag_validation import evidence_pack, payload


def test_partial_answer_must_identify_missing_points_and_keep_references(tmp_path):
    pack = evidence_pack(tmp_path)
    body = json.loads(payload())
    body["status"] = "partial"
    body["missing_points"] = ["本次资料未提供后续阶段的具体任务。"]
    output = validate_model_output(json.dumps(body, ensure_ascii=False), pack, task_protocol=True)
    assert output.status == "partial" and output.missing_points
    markdown = render_markdown(output)
    assert "本次尚缺少的依据" in markdown and "[1]" in markdown
    body["missing_points"] = []
    with pytest.raises(ModelOutputError):
        validate_model_output(json.dumps(body), pack, task_protocol=True)


def test_legacy_does_not_silently_accept_new_fields(tmp_path):
    body = json.loads(payload())
    body["missing_points"] = []
    with pytest.raises(ModelOutputError) as error:
        validate_model_output(json.dumps(body), evidence_pack(tmp_path))
    assert error.value.reason == "unknown_field"


@pytest.mark.parametrize("missing", ["bad-type", [""], ["x" * 501], ["缺项"] * 9])
def test_missing_point_schema_is_bounded(tmp_path, missing):
    body = json.loads(payload())
    body.update(status="partial", missing_points=missing)
    with pytest.raises(ModelOutputError):
        validate_model_output(json.dumps(body), evidence_pack(tmp_path), task_protocol=True)


def test_answered_cannot_hide_missing_points(tmp_path):
    body = json.loads(payload())
    body["missing_points"] = ["缺少另一半问题的依据"]
    with pytest.raises(ModelOutputError):
        validate_model_output(json.dumps(body), evidence_pack(tmp_path), task_protocol=True)


def test_task_prompt_preserves_injection_and_role_boundaries():
    prompt = build_task_system_prompt()
    assert TASK_PROMPT_VERSION != "rag-qa-v6"
    assert "不可信数据" in prompt and "missing_points" in prompt
    assert "实验设置" in prompt and "效果指标" in prompt
    assert "不得相互代替" in prompt


@pytest.mark.parametrize("literal", [False, True])
def test_non_numeric_ocr_title_spacing_does_not_change_exact_evidence_quote(tmp_path, literal):
    pack = evidence_pack(tmp_path)
    pack.items[0].text = "案例为 探究 AI 的深度学习，划分三个课时。"
    title = "探究 AI 的深度学习" if literal else "探究AI的深度学习"
    raw = payload(conclusion=[{"text": f"案例“{title}”划分三个课时。", "refs": [1]}],
                  explanation=[], evidence_quotes=[{"ref": 1, "span": 1}])
    output = validate_model_output(raw, pack, task_protocol=True)
    assert output.quotes[1] == pack.items[0].text
    validate_user_condition_boundary(output, pack, question="“探究AI的深度学习”有几个课时？")
    with pytest.raises(ModelOutputError, match="quote_not_found"):
        validate_model_output(payload(conclusion=[{"text": title, "refs": [1]}], explanation=[],
            evidence_quotes=[{"ref": 1, "quote": "探究AI的深度学习"}]), pack, task_protocol=True)


@pytest.mark.parametrize("source,phrase", [
    ("收入1 2元", "收入12元"), ("课程 A B", "课程AB"),
    ("项目：已通过", "项目已通过"), ("课程Model AB", "课程model AB"),
    ("我 阅读五章节", "我阅读五章节"),
])
def test_layout_exception_cannot_merge_numbers_words_or_change_punctuation_case(tmp_path, source, phrase):
    pack = evidence_pack(tmp_path)
    pack.items[0].text = source
    output = validate_model_output(payload(conclusion=[{"text": f"“{phrase}”。", "refs": [1]}],
        explanation=[], evidence_quotes=[{"ref": 1, "span": 1}]), pack, task_protocol=True)
    with pytest.raises(ModelOutputError, match="mixed_user_source"):
        validate_user_condition_boundary(output, pack, question=f"“{phrase}”是什么意思？")
