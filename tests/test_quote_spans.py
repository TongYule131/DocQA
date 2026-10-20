"""连续范围引用不能修补、清洗或拼接非连续原文；旧同步协议不扩容。"""
from copy import deepcopy
import json

import pytest

from app.config import Settings
from app.quote_spans import quote_spans, quote_choices, task_evidence_payload
from app.rag_context import EvidenceItem, EvidencePack, build_evidence
from app.rag_prompts import TASK_PROMPT_VERSION, build_task_user_prompt
from app.rag_validation import ModelOutputError, validate_model_output
from app.question_worker import QuestionWorker
from tests.test_question_jobs import LocalRuntime, TEXT, REPLY, question_fixture, submit


def pack(text):
    item = EvidenceItem(reference_id=1, chunk_id="c1", document_id="doc", parse_version_id="v1", page=1, text=text)
    return EvidencePack(items=[item], candidates=[item], index_id="idx", parse_version_id="v1", truncated=False, context_chars=0, drop_reasons=[])


def quote_protocol_body():
    body = json.loads(REPLY)
    # 此文件单独测试既有引用协议；v6扩展由真实任务链路及专门协议回归验证。
    body.pop("answer_parts",None)
    for fact in body["conclusion"]+body["explanation"]:fact.pop("context",None)
    return body


def reply(span=1):
    body = quote_protocol_body()
    body["evidence_quotes"] = [{"ref": 1, "span": span}]
    return json.dumps(body, ensure_ascii=False)


@pytest.mark.parametrize("text", ["", "中文  OCR , 原文", "a" * 1500, "emoji😀\r\n条件；" * 80])
def test_segmentation_keeps_every_character_and_has_no_overlap(text):
    spans = quote_spans(text)
    assert "".join(spans) == text.replace("\r\n", "\n")
    assert all(0 < len(span) <= 180 for span in spans)


def test_selected_span_returns_untouched_spaces_and_punctuation():
    source = "教学设计依托 ' 认知阶 梯 '， 进而支持学习。"
    output = validate_model_output(reply(), pack(source), task_protocol=True)
    assert output.quotes[1] == source
    # 模型删空格的旧式引述继续严格失败，不能用范围协议为任意字符串纠错。
    body = quote_protocol_body()
    body["evidence_quotes"] = [{"ref": 1, "quote": source.replace("， ", "，")}]
    with pytest.raises(ModelOutputError, match="quote_not_found"):
        validate_model_output(json.dumps(body), pack(source), task_protocol=True)


@pytest.mark.parametrize("span", [True, False, 0, -1, 10, "1", 1.0])
def test_invalid_or_out_of_bounds_choice_fails(span):
    with pytest.raises(ModelOutputError): validate_model_output(reply(span), pack(TEXT), task_protocol=True)


def test_all_choices_are_bounded_and_non_contiguous_shape_fails():
    source = "0123456789" * 100
    choices = quote_choices(source)
    assert choices and all(len(text) <= 600 and text in source for text in choices)
    for span in range(1, len(choices) + 1):
        output = validate_model_output(reply(span), pack(source), task_protocol=True)
        assert output.quotes[1] == choices[span - 1]
    body = quote_protocol_body()
    body["evidence_quotes"] = [{"ref": 1, "spans": [1, 3]}]
    with pytest.raises(ModelOutputError): validate_model_output(json.dumps(body), pack(TEXT), task_protocol=True)
    body["evidence_quotes"] = [{"ref": 1, "quote": source}]
    with pytest.raises(ModelOutputError, match="quote_too_long"):
        validate_model_output(json.dumps(body), pack(source), task_protocol=True)


def test_old_synchronous_protocol_rejects_span_shape():
    with pytest.raises(ModelOutputError) as error: validate_model_output(reply(), pack(TEXT))
    assert error.value.reason == "unknown_field"


def test_input_payload_is_immutable_and_budget_includes_real_span_metadata():
    entries = [{"ref": 1, "content": "x" * 800, "heading": "原文标题"}]
    before = deepcopy(entries)
    parsed = json.loads(build_task_user_prompt(question="问题", document_name="样本", evidence_payload=entries))
    assert parsed["references"][0]["content"][0]["span"] == 1
    assert ["".join(s["segments"].values()) for s in parsed["references"][0]["content"]] == quote_choices(entries[0]["content"])
    assert entries == before
    hit = {"chunk_id": "c1", "document_id": "doc", "parse_version_id": "v1", "page": 1,
           "text": entries[0]["content"], "score": 1.0}
    result = build_evidence({"index_id": "i", "parse_version_id": "v1", "results": [hit]},
                           Settings(), expected_document_id="doc", payload_transform=task_evidence_payload)
    actual = json.dumps(task_evidence_payload(result.pack.payload()), ensure_ascii=False, separators=(",", ":"))
    assert result.pack.context_chars == len(actual)


def test_real_question_worker_publishes_server_selected_contiguous_quote(tmp_path):
    env = question_fixture(tmp_path)
    body=json.loads(REPLY)
    body["evidence_quotes"]=[{"ref":1,"span":1}]
    env.model.handler = lambda *_: json.dumps(body,ensure_ascii=False)
    job, _ = submit(env)
    assert QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(job["id"])
    finished = env.service.get_job(job["id"])
    assert finished["status"] == "succeeded", finished
    result = env.service.get_result(finished["result_id"])
    assert result["answer"]["citations"][0]["quote"] == TEXT
    assert result["answer"]["prompt_version"] == TASK_PROMPT_VERSION
    assert isinstance(json.loads(env.model.calls[0][1])["references"][0]["content"], list)


def test_bounded_choices_cover_each_original_character_without_rewriting():
    source = "".join(chr(0x4e00 + i) for i in range(1800))
    choices = quote_choices(source)
    positions = set()
    for choice in choices:
        start = source.index(choice)
        positions.update(range(start, start + len(choice)))
    assert positions == set(range(len(source)))
