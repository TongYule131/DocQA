"""多路证据规划的合成离线正反例，不冒充真实模型语义通过。"""
import json

import pytest

from app.config import Settings
from app.rag_context import build_evidence
from app.retrieval_planning import plan_retrieval, input_coverage_hints
from app.schemas import Chunk, SourceLocation


def chunk(i, text, heading=None, version="v1"):
    return Chunk(id=f"c{i}", document_id="doc", page=i + 1, text=text,
                 parse_version_id=version, order_index=i, block_id=f"b{i}",
                 heading_path=heading, chunk_type="text", sources=[SourceLocation(format="pdf", page=i + 1)])


def vector_result(chunks):
    return {"index_id": "idx", "parse_version_id": "v1", "results": [{
        "chunk_id": c.id, "document_id": c.document_id, "parse_version_id": c.parse_version_id,
        "page": c.page, "text": c.text, "score": 0.99 - i / 100,
        "chunk_type": c.chunk_type, "heading_path": c.heading_path,
        "order_index": c.order_index, "block_id": c.block_id,
        "sources": [s.model_dump() for s in c.sources]} for i, c in enumerate(chunks)]}


def test_direct_definition_and_explicit_section_bodies_enter_tight_context_before_summary():
    chunks = [chunk(0, "本报告概述三类机制与传输网格的定位，具体定义见正文。", "摘要"),
        chunk(1, "第一机制通过统一接口，让不同设备按照同一协议交换数据。", "12 总体机制 / 12.1 接口机制"),
        chunk(2, "第二机制通过资源调度，实现不同节点之间的协作和负载均衡。", "12 总体机制 / 12.2 调度机制"),
        chunk(3, "传输网格是指设备之间传递数据和指令的连接基础设施，而不是调度规则。", "12 总体机制 / 12.3 连接机制")]
    planned = plan_retrieval('第12节提出哪些机制？“传输网格”应如何定位？', vector_result([chunks[0]]), chunks)
    assert planned["evidence_order"][0] == "c3"
    assert set(planned["evidence_order"][:3]) == {"c1", "c2", "c3"}
    assert planned["planning"]["selection_reasons"]["c3"] == "named_direct_definition"
    built = build_evidence(planned, Settings(rag_context_k=3, rag_context_max_chars=2000), expected_document_id="doc")
    assert {item.chunk_id for item in built.pack.items} == {"c1", "c2", "c3"}
    assert built.pack.context_chars <= 2000


def test_definition_priority_is_not_triggered_by_an_unrelated_definition_or_page_number():
    chunks = [chunk(0, "传输网格的连接范围限于园区。", "运行说明"),
              chunk(1, "调度中心是指分配计算任务的控制设施。", "其他定义")]
    planned = plan_retrieval('“传输网格”的定位是什么？', vector_result([chunks[0]]), chunks)
    assert "named_direct_definition" not in planned["planning"]["selection_reasons"].values()


def test_author_position_question_reserves_actual_conclusion_without_deciding_its_direction():
    chunks = [chunk(0, "接口约束和适配成本是常见问题，背景研究对此有许多讨论。", "背景"),
              chunk(1, "本报告认为，适配存在成本，但不应取消接口约束，应改善工具支持。", "5 结论")]
    planned = plan_retrieval("有观点认为适配有成本，因此应取消接口约束，这是否符合作者观点？",
                             vector_result([chunks[0]]), chunks)
    assert planned["evidence_order"][0] == "c1"
    assert planned["planning"]["selection_reasons"]["c1"] == "author_position_conclusion"
    assert planned["planning"]["semantic_completeness_verified"] is False


def test_keyword_branch_recovers_unretrieved_definition_and_caption_context():
    chunks = [chunk(0, "研究背景与宏观政策。"),
              chunk(1, "模型由外环原则、内环流程与实践要素组成。"),
              chunk(2, "图1 教学模型"), chunk(3, "实践要素包含生活化情境、认知阶梯和实践任务。")]
    planned = plan_retrieval("教学模型包含哪些实践要素？", vector_result([chunks[2]]), chunks)
    ids = [hit["chunk_id"] for hit in planned["results"]]
    assert "c1" in ids and "c3" in ids
    assert planned["planning"]["semantic_completeness_verified"] is False
    assert {hit["text"] for hit in planned["results"]} <= {c.text for c in chunks}


def test_question_parts_preserve_distinct_subjects_before_fill():
    chunks = [chunk(0, "甲项目投入预算100万元。", "项目甲"),
              chunk(1, "预算管理原则。", "项目甲"),
              chunk(2, "乙项目实施条件是完成培训。", "项目乙"),
              chunk(3, "实施过程说明。", "项目乙")]
    planned = plan_retrieval("甲项目投入预算是多少？乙项目实施条件是什么？",
                             vector_result([chunks[0], chunks[1]]), chunks, max_candidates=8)
    assert set(planned["evidence_order"][:2]) == {"c0", "c2"}
    assert len(planned["planning"]["question_parts"]) == 2


def test_rank_fusion_order_survives_evidence_build_and_budget():
    chunks = [chunk(0, "泛化讨论，没有目标。"), chunk(1, "目标指标的完整数值为18万元。")]
    planned = plan_retrieval("目标指标数值是多少？", vector_result([chunks[0]]), chunks)
    settings = Settings(rag_context_k=1)
    evidence = build_evidence(planned, settings, expected_document_id="doc")
    assert evidence.pack.items[0].chunk_id == "c1"
    assert evidence.pack.items[0].text == chunks[1].text
    assert len(evidence.pack.serialized()) <= settings.rag_context_max_chars


def test_table_header_and_footnote_are_not_trimmed():
    text = "期间：2025年；单位：万元\n主体 | 营收\n甲 | 100\n注：未经审计。"
    c = chunk(0, text)
    c.chunk_type = "table_rows"
    planned = plan_retrieval("甲的营收是多少？", vector_result([]), [c])
    assert planned["results"][0]["text"] == text


def test_cross_version_candidates_are_rejected():
    with pytest.raises(ValueError, match="scope"):
        plan_retrieval("问题", vector_result([]), [chunk(0, "另一版本", version="v2")])


def test_evidence_plan_is_finite_and_has_no_extra_generated_facts():
    chunks = [chunk(i, f"目标指标{i}的原文。", "同章节") for i in range(100)]
    plan = plan_retrieval("目标指标", vector_result(chunks[:8]), chunks, max_candidates=12)
    assert len(plan["results"]) <= 12
    assert len(set(plan["evidence_order"])) == len(plan["results"])
    json.dumps(plan, ensure_ascii=False, allow_nan=False)


def test_complete_reading_is_only_claimed_when_all_metadata_fits():
    chunks = [chunk(0, "第一章概述。"), chunk(1, "最后一章的重要例外。")]
    complete = plan_retrieval("概述全文主要内容", vector_result(chunks[:1]), chunks,
                             fulltext_budget=10000)
    assert complete["planning"]["route"] == "complete_indexed_text"
    assert complete["evidence_order"] == ["c0", "c1"]
    limited = plan_retrieval("概述全文主要内容", vector_result(chunks[:1]), chunks,
                            fulltext_budget=10)
    assert limited["planning"].get("route") != "complete_indexed_text"


def test_keyword_candidates_use_actual_cosine_threshold_without_extra_query():
    chunks = [chunk(0, "其他资料。"), chunk(1, "目标指标为18万元。")]
    vector = vector_result(chunks[:1])
    vector["candidate_scores"] = {"c0": 0.99, "c1": 0.8}
    planned = plan_retrieval("目标指标是多少？", vector, chunks)
    assert "candidate_scores" not in planned
    evidence = build_evidence(planned, Settings(rag_min_score=0.5), expected_document_id="doc")
    assert "c1" in {item.chunk_id for item in evidence.pack.items}
    assert next(item for item in evidence.pack.items if item.chunk_id == "c1").score == 0.8


def test_named_elements_link_to_explanatory_body_before_background_uses_budget():
    chunks=[chunk(0,"模型层次和关键要素是重要研究问题。","背景"),
            chunk(1,"模型以目标为外环，以观察解释行动为内环；关键要素是'生活情境'和'认知阶梯'。","模型"),
            chunk(2,"生活情境通过类比提供经验载体。","模型 / 生活情境"),
            chunk(3,"认知阶梯以分层任务推动从具体到抽象。","模型 / 认知阶梯")]
    planned=plan_retrieval("模型有哪些层次和关键要素，如何发挥作用？",vector_result(chunks[:2]),chunks)
    built=build_evidence(planned,Settings(rag_context_k=3),expected_document_id="doc")
    assert {i.chunk_id for i in built.pack.items}=={"c1","c2","c3"}


def test_stage_ranges_cover_later_lessons_despite_embedded_headings():
    chunks=[chunk(0,"背景。1. 认识探头。本课时通过感官类比，记录温度。","教学"),
            chunk(1,"2. 判断规则。本课时通过门卫类比，编写判断任务。","教学"),
            chunk(2,"3. 综合应用","教学"),
            chunk(3,"第三课时以菜园为情境，学生调试灌溉系统。","教学"),
            chunk(4,"结论与参考文献。","结束")]
    planned=plan_retrieval("三个课时分别教什么、用什么类比、实践任务是什么？",vector_result(chunks[:1]),chunks)
    built=build_evidence(planned,Settings(rag_context_k=3),expected_document_id="doc")
    assert {i.chunk_id for i in built.pack.items}=={"c0","c1","c3"}


def test_bad_version_is_rejected_even_when_low_scored_and_would_be_filtered():
    chunks=[chunk(0,"目标数值为18万元。"),chunk(1,"背景。")]
    vector=vector_result(chunks)
    vector["results"][1]["parse_version_id"]="wrong-version"
    vector["results"][1]["score"]=-0.5
    with pytest.raises(ValueError,match="scope"):
        plan_retrieval("目标数值？",vector,chunks,max_candidates=1)


def test_input_hints_only_mark_actually_packed_ids_and_are_not_semantic_verdict():
    hints=input_coverage_hints({"aspects":[{"id":"part-1","candidate_ids":["c1","c2"]}]},{"c2"})
    assert hints["aspects"][0]["selected_chunk_ids"]==["c2"]
    assert hints["heuristic_only"] and not hints["semantic_completeness_verified"]


@pytest.mark.parametrize("first_heading",["（一）生活情境","（ 一 ） 生活情境"])
def test_legacy_missing_heading_metadata_gets_search_hints_without_rewriting_sources(first_heading):
    chunks=[chunk(0,"模型含'生活情境'与'认知阶梯'两个要素。"),
            chunk(1,first_heading),chunk(2,"生活情境通过类比提供经验载体。"),
            chunk(3,"（二）认知阶梯"),chunk(4,"认知阶梯通过分层任务促进抽象理解。")]
    planned=plan_retrieval("模型要素及其作用？",vector_result(chunks[:1]),chunks)
    assert planned["planning"]["derived_search_heading_count"]>=2
    assert planned["planning"]["selection_reasons"]["c2"]=="named_element_explanation"
    assert all(hit["heading_path"] is None for hit in planned["results"])
    assert all(c.heading_path is None for c in chunks)
