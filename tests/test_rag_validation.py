"""RAG 模型输出协议与后端引用校验的离线测试（工程包 C/D）。

对应验收矩阵 R07/R08/R09/R10/R11/R12 的离线部分，以及 R14 的模型输出两侧边界。

这里的每条正例都必须真正经过生产校验器（`validate_model_output`）与生产渲染器
（`render_markdown`），并使用与生产相同的证据包结构；反例必须真的被拒绝，
不允许用“替代整个生产逻辑的假函数”证明引用校验有效。

再次强调：本文件只证明**结构与来源可追溯**。像“原文为 21、答案写 22 且引用合法”
这类语义错误**不会**在这里被拦下，必须由语义评估（scripts/evaluate_rag.py）发现。
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.rag_context import build_evidence
from app.rag_validation import (
    MAX_FACT_TEXT_CHARS,
    MAX_QUOTE_CHARS,
    ModelOutputError,
    parse_model_output,
    render_markdown,
    validate_model_output,
)

DOC = "doc-val-1"


def settings_for(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path)


def evidence_pack(tmp_path, hits=None):
    """构造与生产一致的证据包：两条真实正文，页码分别为第 2、3 页。"""
    hits = hits if hits is not None else [
        {"chunk_id": "c1", "document_id": DOC, "page": 2,
         "text": "全书内容分为21个部分，第一部分为总述。", "score": 0.9,
         "parse_version_id": "v-a", "chunk_type": "text", "heading_path": None,
         "order_index": 0,
         "sources": [{"format": "pdf", "page": 2, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4},
                      "coord_origin": "BOTTOMLEFT", "coord_unit": "pt"}]},
        {"chunk_id": "c2", "document_id": DOC, "page": 3,
         "text": "第二部分收录了统计表格，单位为万元。", "score": 0.8,
         "parse_version_id": "v-a", "chunk_type": "text", "heading_path": None,
         "order_index": 1,
         "sources": [{"format": "pdf", "page": 3}]},
    ]
    result = {"index_id": "idx-a", "parse_version_id": "v-a", "document_id": DOC,
              "is_old_version": False, "is_legacy": False, "chunk_count": len(hits),
              "results": hits}
    build = build_evidence(result, settings_for(tmp_path))
    assert build.fatal_reason is None
    assert build.pack.items, "证据包必须非空，否则本文件的校验测试没有意义"
    return build.pack


def payload(**overrides) -> str:
    """默认正例：一条结论 + 一条说明，引用编号 1。"""
    body = {
        "status": "answered",
        "conclusion": [{"text": "全书内容分为21个部分。", "refs": [1]}],
        "explanation": [{"text": "该结论出自第一部分的总述。", "refs": [1]}],
        "clarification_questions": [],
        "evidence_quotes": [{"ref": 1, "quote": "全书内容分为21个部分"}],
    }
    body.update(overrides)
    return json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 正例：真正通过生产校验器
# ---------------------------------------------------------------------------
def test_valid_answered_output_is_accepted_and_rendered(tmp_path):
    pack = evidence_pack(tmp_path)
    result = validate_model_output(payload(), pack)
    assert result.status == "answered"
    assert result.used_refs() == {1}
    markdown = render_markdown(result)
    # 事实后紧跟由后端生成的引用标记，不集中堆在末尾。
    assert "- 全书内容分为21个部分。[1]" in markdown
    assert markdown.startswith("## 结论")
    assert "## 依据与说明" in markdown


def test_multiple_references_are_rendered_in_order(tmp_path):
    pack = evidence_pack(tmp_path)
    raw = json.dumps({
        "status": "answered",
        "conclusion": [{"text": "全书分为21个部分，第二部分收录统计表格。", "refs": [2, 1]}],
        "explanation": [],
        "clarification_questions": [],
        "evidence_quotes": [{"ref": 1, "quote": "全书内容分为21个部分"},
                            {"ref": 2, "quote": "第二部分收录了统计表格"}],
    }, ensure_ascii=False)
    result = validate_model_output(raw, pack)
    assert result.used_refs() == {1, 2}
    assert "[1][2]" in render_markdown(result)


def test_clarification_output_is_accepted(tmp_path):
    pack = evidence_pack(tmp_path)
    raw = json.dumps({
        "status": "clarification_needed",
        "conclusion": [],
        "explanation": [{"text": "资料只说明全书分为21个部分，未说明具体统计口径。", "refs": [2]}],
        "clarification_questions": ["您关心的是哪个部分的统计表格？"],
        "evidence_quotes": [{"ref": 2, "quote": "第二部分收录了统计表格"}],
    }, ensure_ascii=False)
    result = validate_model_output(raw, pack)
    assert result.status == "clarification_needed"
    markdown = render_markdown(result)
    # 澄清回复不能被渲染成“已经有明确结论”。
    assert "## 结论" not in markdown
    assert "需要先补充以下信息" in markdown
    assert "1. 您关心的是哪个部分的统计表格？" in markdown


def test_insufficient_evidence_output_is_accepted(tmp_path):
    pack = evidence_pack(tmp_path)
    raw = json.dumps({"status": "insufficient_evidence", "conclusion": [], "explanation": [],
                      "clarification_questions": [], "evidence_quotes": []}, ensure_ascii=False)
    result = validate_model_output(raw, pack)
    assert result.status == "insufficient_evidence"
    assert result.used_refs() == set()


def test_json_code_fence_is_accepted(tmp_path):
    """只兼容“整个输出被一对 json 代码围栏包裹”的格式。"""
    pack = evidence_pack(tmp_path)
    fenced = "```json\n" + payload() + "\n```"
    assert validate_model_output(fenced, pack).status == "answered"
    plain_fence = "```\n" + payload() + "\n```"
    assert validate_model_output(plain_fence, pack).status == "answered"


def test_quote_may_unify_crlf(tmp_path):
    """引述允许统一 CRLF：证据正文为 CRLF 时，模型按 LF 引述仍应通过。"""
    pack = evidence_pack(tmp_path, hits=[
        {"chunk_id": "c1", "document_id": DOC, "page": 2,
         "text": "第一行事实\r\n第二行事实", "score": 0.9, "parse_version_id": "v-a",
         "chunk_type": "text", "heading_path": None, "order_index": 0, "sources": []}])
    raw = json.dumps({"status": "answered",
                      "conclusion": [{"text": "包含两行事实。", "refs": [1]}],
                      "explanation": [], "clarification_questions": [],
                      "evidence_quotes": [{"ref": 1, "quote": "第一行事实\n第二行事实"}]},
                     ensure_ascii=False)
    assert validate_model_output(raw, pack).status == "answered"


# ---------------------------------------------------------------------------
# R07 伪造引用与未入选引用
# ---------------------------------------------------------------------------
def test_unknown_reference_is_rejected(tmp_path):
    """引用不存在的编号必须拒绝。"""
    pack = evidence_pack(tmp_path)
    raw = payload(conclusion=[{"text": "不存在的引用。", "refs": [9]}],
                  evidence_quotes=[{"ref": 9, "quote": "全书内容分为21个部分"}])
    _assert_rejected(raw, pack, "unknown_reference")


def test_reference_only_in_candidates_is_rejected(tmp_path):
    """候选中存在但未送入模型的编号不可引用。

    这里用 rag_context_k=1 使第二条候选只存在于候选中；它的编号从未分配。
    """
    hits = [
        {"chunk_id": "c1", "document_id": DOC, "page": 2, "text": "第一条正文", "score": 0.9,
         "parse_version_id": "v-a", "chunk_type": "text", "heading_path": None,
         "order_index": 0, "sources": []},
        {"chunk_id": "c2", "document_id": DOC, "page": 3, "text": "第二条正文", "score": 0.8,
         "parse_version_id": "v-a", "chunk_type": "text", "heading_path": None,
         "order_index": 1, "sources": []},
    ]
    result = {"index_id": "idx-a", "parse_version_id": "v-a", "document_id": DOC,
              "is_old_version": False, "is_legacy": False, "chunk_count": 2, "results": hits}
    settings = Settings(data_dir=tmp_path, rag_context_k=1)
    build = build_evidence(result, settings)
    assert [item.chunk_id for item in build.pack.items] == ["c1"]
    assert len(build.pack.candidates) == 2
    raw = payload(conclusion=[{"text": "引用了未入选的块。", "refs": [2]}],
                  evidence_quotes=[{"ref": 2, "quote": "第二条正文"}])
    _assert_rejected(raw, build.pack, "unknown_reference")


def test_cross_document_and_cross_version_hits_never_reach_validation(tmp_path):
    """跨文档/跨版本块在证据构建阶段就受控失败，不会进入校验。"""
    for bad_hit in (
        {"chunk_id": "c9", "document_id": "doc-other", "page": 2, "text": "混入正文",
         "score": 0.9, "parse_version_id": "v-a", "chunk_type": "text", "heading_path": None,
         "order_index": 0, "sources": []},
        {"chunk_id": "c8", "document_id": DOC, "page": 2, "text": "混入正文", "score": 0.9,
         "parse_version_id": "v-b", "chunk_type": "text", "heading_path": None,
         "order_index": 0, "sources": []},
    ):
        result = {"index_id": "idx-a", "parse_version_id": "v-a", "document_id": DOC,
                  "is_old_version": False, "is_legacy": False, "chunk_count": 1,
                  "results": [bad_hit]}
        build = build_evidence(result, settings_for(tmp_path))
        assert build.fatal_reason is not None
        assert build.pack.items == []


# ---------------------------------------------------------------------------
# R08 原文引述
# ---------------------------------------------------------------------------
def test_quote_with_changed_number_is_rejected(tmp_path):
    """把 21 改成 22 的引述不是原文连续子串，必须拒绝。"""
    pack = evidence_pack(tmp_path)
    raw = payload(evidence_quotes=[{"ref": 1, "quote": "全书内容分为22个部分"}])
    _assert_rejected(raw, pack, "quote_not_found")


def test_quote_with_removed_punctuation_is_rejected(tmp_path):
    """删标点制造匹配必须拒绝。"""
    pack = evidence_pack(tmp_path)
    raw = payload(evidence_quotes=[{"ref": 1, "quote": "全书内容分为21个部分第一部分为总述"}])
    _assert_rejected(raw, pack, "quote_not_found")


def test_quote_splicing_discontinuous_text_is_rejected(tmp_path):
    """拼接不相邻片段必须拒绝。"""
    pack = evidence_pack(tmp_path, hits=[
        {"chunk_id": "c1", "document_id": DOC, "page": 2,
         "text": "甲段内容。中间还有别的内容。乙段内容。", "score": 0.9,
         "parse_version_id": "v-a", "chunk_type": "text", "heading_path": None,
         "order_index": 0, "sources": []}])
    raw = json.dumps({"status": "answered",
                      "conclusion": [{"text": "拼接的结论。", "refs": [1]}],
                      "explanation": [], "clarification_questions": [],
                      "evidence_quotes": [{"ref": 1, "quote": "甲段内容。乙段内容。"}]},
                     ensure_ascii=False)
    _assert_rejected(raw, pack, "quote_not_found")


def test_quote_from_other_chunk_is_rejected(tmp_path):
    """移花接木：用另一条证据的正文充当本条引述必须拒绝。"""
    pack = evidence_pack(tmp_path)
    raw = payload(evidence_quotes=[{"ref": 1, "quote": "第二部分收录了统计表格"}])
    _assert_rejected(raw, pack, "quote_not_found")


@pytest.mark.parametrize("quote", ["", "   ", "\n\t "])
def test_empty_quote_is_rejected(tmp_path, quote):
    """空或纯空白引述必须拒绝。"""
    pack = evidence_pack(tmp_path)
    raw = payload(evidence_quotes=[{"ref": 1, "quote": quote}])
    _assert_rejected(raw, pack, "empty_quote")


def test_over_long_quote_is_rejected(tmp_path):
    """引述超长必须拒绝，而不是截断成“合格引述”。"""
    long_text = "长正文" * 400
    pack = evidence_pack(tmp_path, hits=[
        {"chunk_id": "c1", "document_id": DOC, "page": 2, "text": long_text, "score": 0.9,
         "parse_version_id": "v-a", "chunk_type": "text", "heading_path": None,
         "order_index": 0, "sources": []}])
    raw = json.dumps({"status": "answered",
                      "conclusion": [{"text": "长结论。", "refs": [1]}],
                      "explanation": [], "clarification_questions": [],
                      "evidence_quotes": [{"ref": 1, "quote": long_text[:MAX_QUOTE_CHARS + 5]}]},
                     ensure_ascii=False)
    _assert_rejected(raw, pack, "quote_too_long")


# ---------------------------------------------------------------------------
# R09 输出协议
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,reason", [
    ("", "invalid_json"),
    ("   ", "invalid_json"),
    ("这不是 JSON", "invalid_json"),
    ("[1, 2, 3]", "not_object"),
    ('{"status": "answered",}', "invalid_json"),
    ("前言\n{\"status\":\"answered\"}\n后记", "invalid_json"),
    ("```json\n{\"status\":\"answered\"}\n```\n多余的后记", "invalid_json"),
    ("```python\n{}\n```", "invalid_json"),
])
def test_bad_json_is_rejected(tmp_path, raw, reason):
    pack = evidence_pack(tmp_path)
    _assert_rejected(raw, pack, reason)


def test_duplicate_json_key_is_rejected(tmp_path):
    """重复 JSON 键必须拒绝，不允许后者静默覆盖前者。"""
    pack = evidence_pack(tmp_path)
    raw = ('{"status":"answered","status":"insufficient_evidence","conclusion":[],'
           '"explanation":[],"clarification_questions":[],"evidence_quotes":[]}')
    _assert_rejected(raw, pack, "duplicate_key")


def test_nan_constant_is_rejected(tmp_path):
    """NaN 不是合法 JSON 数值，必须拒绝而不是当成 0 或字符串。"""
    pack = evidence_pack(tmp_path)
    raw = ('{"status":"answered","conclusion":[{"text":"x","refs":[1]}],"explanation":[],'
           '"clarification_questions":[],"evidence_quotes":[{"ref":1,"quote":NaN}]}')
    _assert_rejected(raw, pack, "invalid_json")


def test_unknown_and_missing_fields_are_rejected(tmp_path):
    pack = evidence_pack(tmp_path)
    body = json.loads(payload())
    body["extra"] = "多余字段"
    _assert_rejected(json.dumps(body, ensure_ascii=False), pack, "unknown_field")

    body = json.loads(payload())
    del body["evidence_quotes"]
    _assert_rejected(json.dumps(body, ensure_ascii=False), pack, "missing_field")

    body = json.loads(payload())
    body["conclusion"] = [{"text": "事实", "refs": [1], "note": "多余"}]
    _assert_rejected(json.dumps(body, ensure_ascii=False), pack, "unknown_field")


@pytest.mark.parametrize("ref", ["1", 1.0, True, None, 0, -1])
def test_non_positive_integer_reference_is_rejected(tmp_path, ref):
    """引用编号必须是真正的正整数：字符串、浮点、布尔值一律拒绝。"""
    pack = evidence_pack(tmp_path)
    body = json.loads(payload())
    body["conclusion"][0]["refs"] = [ref]
    body["evidence_quotes"] = [{"ref": ref, "quote": "全书内容分为21个部分"}]
    raw = json.dumps(body, ensure_ascii=False)
    with pytest.raises(ModelOutputError) as error:
        validate_model_output(raw, pack)
    assert error.value.reason in {"bad_type", "unknown_reference"}


@pytest.mark.parametrize("status", ["", "unknown", "ANSWERED", None, 1])
def test_bad_status_is_rejected(tmp_path, status):
    pack = evidence_pack(tmp_path)
    body = json.loads(payload())
    body["status"] = status
    _assert_rejected(json.dumps(body, ensure_ascii=False), pack,
                     "bad_type" if not isinstance(status, str) else "bad_status")


def test_self_written_citation_marker_is_rejected(tmp_path):
    """模型自写 [1] 引用标记必须拒绝：编号只能由后端渲染。"""
    pack = evidence_pack(tmp_path)
    raw = payload(conclusion=[{"text": "全书内容分为21个部分。[1]", "refs": [1]}])
    _assert_rejected(raw, pack, "self_written_marker")


def test_over_long_fact_is_rejected(tmp_path):
    """事实超长必须拒绝，不能截断成“合格答案”。"""
    pack = evidence_pack(tmp_path)
    raw = payload(conclusion=[{"text": "长" * (MAX_FACT_TEXT_CHARS + 1), "refs": [1]}])
    _assert_rejected(raw, pack, "text_too_long")


def test_too_many_facts_are_rejected(tmp_path):
    """事实条目总数超过 10 条必须拒绝。"""
    pack = evidence_pack(tmp_path)
    facts = [{"text": f"事实 {index}", "refs": [1]} for index in range(11)]
    _assert_rejected(payload(conclusion=facts), pack, "facts_too_many")


# ---------------------------------------------------------------------------
# R10 引用完整性
# ---------------------------------------------------------------------------
def test_fact_without_reference_is_rejected(tmp_path):
    """每条事实都必须有非空 refs。"""
    pack = evidence_pack(tmp_path)
    raw = payload(conclusion=[{"text": "没有引用的事实。", "refs": []}])
    _assert_rejected(raw, pack, "empty_reference_list")


def test_missing_quote_for_used_reference_is_rejected(tmp_path):
    """被使用的编号缺少引述必须拒绝。"""
    pack = evidence_pack(tmp_path)
    raw = payload(evidence_quotes=[])
    _assert_rejected(raw, pack, "missing_quote")


def test_quote_for_unused_reference_is_rejected(tmp_path):
    """未被任何事实使用的引述必须拒绝，不能把候选片段冒充已引用来源。"""
    pack = evidence_pack(tmp_path)
    raw = payload(evidence_quotes=[{"ref": 1, "quote": "全书内容分为21个部分"},
                                   {"ref": 2, "quote": "第二部分收录了统计表格"}])
    _assert_rejected(raw, pack, "unused_quote")


def test_conflicting_duplicate_quote_is_rejected(tmp_path):
    """同一编号的重复引述定义必须拒绝。"""
    pack = evidence_pack(tmp_path)
    raw = payload(evidence_quotes=[{"ref": 1, "quote": "全书内容分为21个部分"},
                                   {"ref": 1, "quote": "全书内容分为21个部分，第一部分为总述。"}])
    _assert_rejected(raw, pack, "duplicate_quote")


def test_duplicate_reference_inside_one_fact_is_rejected(tmp_path):
    """同一事实内重复引用同一编号必须拒绝，避免重复渲染标记。"""
    pack = evidence_pack(tmp_path)
    raw = payload(conclusion=[{"text": "重复引用。", "refs": [1, 1]}])
    _assert_rejected(raw, pack, "duplicate_quote")


# ---------------------------------------------------------------------------
# 状态约束（R11/R12）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("overrides,reason", [
    ({"conclusion": []}, "empty_conclusion"),
    ({"clarification_questions": ["需要补充条件吗？"]}, "status_conflict"),
])
def test_answered_state_constraints(tmp_path, overrides, reason):
    pack = evidence_pack(tmp_path)
    _assert_rejected(payload(**overrides), pack, reason)


@pytest.mark.parametrize("overrides,reason", [
    ({"conclusion": [{"text": "不该有结论。", "refs": [1]}]}, "status_conflict"),
    ({"explanation": []}, "empty_explanation"),
    ({"clarification_questions": []}, "question_count"),
    ({"clarification_questions": ["一", "二", "三"]}, "question_count"),
])
def test_clarification_state_constraints(tmp_path, overrides, reason):
    pack = evidence_pack(tmp_path)
    body = {"status": "clarification_needed", "conclusion": [],
            "explanation": [{"text": "缺少条件。", "refs": [1]}],
            "clarification_questions": ["需要补充什么条件？"],
            "evidence_quotes": [{"ref": 1, "quote": "全书内容分为21个部分"}]}
    body.update(overrides)
    _assert_rejected(json.dumps(body, ensure_ascii=False), pack, reason)


@pytest.mark.parametrize("overrides,reason", [
    ({"conclusion": [{"text": "不该有结论。", "refs": [1]}]}, "status_conflict"),
    ({"explanation": [{"text": "不该有说明。", "refs": [1]}]}, "status_conflict"),
    ({"clarification_questions": ["不该有问题？"]}, "status_conflict"),
])
def test_insufficient_state_must_be_empty(tmp_path, overrides, reason):
    pack = evidence_pack(tmp_path)
    body = {"status": "insufficient_evidence", "conclusion": [], "explanation": [],
            "clarification_questions": [], "evidence_quotes": []}
    body.update(overrides)
    _assert_rejected(json.dumps(body, ensure_ascii=False), pack, reason)


def test_parse_model_output_reports_stable_reason_codes(tmp_path):
    """原因码稳定，且不把原文内容带回错误对象，可直接用于日志而不泄露资料。"""
    with pytest.raises(ModelOutputError) as error:
        parse_model_output('{"a": NaN}')
    assert error.value.reason == "invalid_json"
    assert "NaN" not in str(error.value.detail)


def _assert_rejected(raw: str, pack, reason: str) -> None:
    """断言生产校验器真的拒绝该输出，并给出预期原因码。"""
    with pytest.raises(ModelOutputError) as error:
        validate_model_output(raw, pack)
    assert error.value.reason == reason, f"期望 {reason}，实际 {error.value.reason}"
