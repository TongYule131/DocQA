"""来源混用边界的合成离线回归；拒绝错误输出不等于在线语义通过。"""
import json

import pytest

from app.rag_validation import ModelOutputError, validate_model_output, validate_user_condition_boundary
from test_rag_api import DOC, Gateway, indexed_client
from test_rag_validation import evidence_pack, payload


@pytest.mark.parametrize("field", ["conclusion", "explanation"])
@pytest.mark.parametrize("opening,closing", [("“", "”"), ("‘", "’"), ("「", "」"),
                                            ("『", "』"), ('"', '"')])
def test_user_quote_cannot_inherit_document_reference(tmp_path, field, opening, closing):
    pack = evidence_pack(tmp_path)
    raw = payload(**{field: [{"text": f"用户提到{opening}我只阅读了三个章节{closing}。", "refs": [1]}]})
    output = validate_model_output(raw, pack)
    with pytest.raises(ModelOutputError) as caught:
        validate_user_condition_boundary(output, pack, question="我只阅读了三个章节，请说明年鉴结构。")
    assert caught.value.reason == "mixed_user_source"


def test_quote_present_in_its_cited_document_remains_valid(tmp_path):
    pack = evidence_pack(tmp_path)
    output = validate_model_output(payload(conclusion=[{
        "text": "资料写明“全书内容分为21个部分”。", "refs": [1]}]), pack)
    validate_user_condition_boundary(output, pack, question="“全书内容分为21个部分”在哪里说明？")


def test_quote_in_another_uncited_chunk_does_not_support_this_fact(tmp_path):
    pack = evidence_pack(tmp_path)
    output = validate_model_output(payload(conclusion=[{
        "text": "“第二部分收录了统计表格”。", "refs": [1]}]), pack)
    with pytest.raises(ModelOutputError) as caught:
        validate_user_condition_boundary(output, pack, question="第二部分收录了统计表格，请解释。")
    assert caught.value.reason == "mixed_user_source"


def test_clarification_questions_may_ask_about_user_condition(tmp_path):
    pack = evidence_pack(tmp_path)
    raw = payload(status="clarification_needed", conclusion=[],
                  clarification_questions=["您所说的“我只阅读了三个章节”指哪些章节？"])
    output = validate_model_output(raw, pack)
    validate_user_condition_boundary(output, pack, question="我只阅读了三个章节，年鉴还包括哪些内容？")


def test_guard_does_not_claim_general_semantic_validation(tmp_path):
    # 数字造假并未被此处的引号边界涵盖；保留失败能力，不能把它当通用语义判官。
    pack = evidence_pack(tmp_path)
    output = validate_model_output(payload(conclusion=[{
        "text": "全书分为99个部分。", "refs": [1]}]), pack)
    validate_user_condition_boundary(output, pack, question="全书分为多少个部分？")


def test_api_rejects_mixed_source_without_retry_or_disclosure(tmp_path, monkeypatch, caplog):
    gateway = Gateway()
    gateway.install(monkeypatch)
    body = json.loads(payload())
    body["explanation"] = [{"text": "用户说“我只阅读了三个章节”，因此还需补充条件。", "refs": [1]}]
    gateway.generation_handler = lambda request: gateway.completion(json.dumps(body, ensure_ascii=False))
    for client, _ in indexed_client(tmp_path, gateway):
        before_embedding = len(gateway.embedding_requests)
        with caplog.at_level("WARNING"):
            response = client.post(f"/api/documents/{DOC}/questions", json={
                "question": "我只阅读了三个章节，请说明年鉴结构。"})
        assert response.status_code == 502
        assert response.json()["code"] == "rag_output_invalid"
        assert "我只阅读" not in response.text
        assert "我只阅读" not in caplog.text
        assert "mixed_user_source" in caplog.text
        assert len(gateway.generation_requests) == 1
        assert len(gateway.embedding_requests) == before_embedding + 1
