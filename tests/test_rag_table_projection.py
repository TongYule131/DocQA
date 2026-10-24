"""表格结构入模、预算优先级及持久化恢复完整链路；全部使用本地桩。"""
import json

import pytest

from app.chunking import ChunkingConfig, chunk_document
from app.config import Settings
from app.document_normalizer import NormalizedDocument
from app.quote_spans import task_evidence_payload
from app.rag_context import build_evidence
from app.rag_table_projection import table_projection, table_structure_signature
from app.repository import now_iso
from tests.fixtures.rag_quality.table_projection import cases, cell
from tests.test_question_jobs import question_fixture, submit
from tests.test_question_worker import worker


def chunks_for(block, **options):
    config = ChunkingConfig(max_chars=options.get("max_chars", 2000), overlap_chars=20,
                           table_rows_per_chunk=options.get("rows", 2))
    document = NormalizedDocument(document_id=block.document_id, format="xlsx",
                                  blocks=[block], page_count=1)
    chunks = chunk_document(document, config)
    for chunk in chunks:
        chunk.parse_version_id = block.parse_version_id
    return chunks


@pytest.mark.parametrize("case", cases(), ids=lambda case: case["id"])
def test_frozen_header_structure_without_semantic_inference(case):
    for chunk in chunks_for(case["block"]):
        projection = table_projection(chunk, case["block"])
        assert projection["status"] == "available", projection
        assert projection["header_status"] == case["expected"]["header_status"]
        assert projection["header_rows"] == case["expected"]["header_rows"]
        assert projection["condition_scope_verified"] is False
        assert all(note["applicability"] == "unresolved" for note in projection["footnotes"])


def test_duplicate_values_blank_zero_and_literal_markers_remain_distinct():
    block = cases()[0]["block"]
    projection = table_projection(chunks_for(block, rows=5)[0], block)
    assert projection["rows"][2]["values"] == ["收入", "9", "9"]
    assert projection["rows"][3]["values"] == ["退款", "", "0"]
    assert [4, 2] in projection["empty_cells"] and [4, 3] not in projection["empty_cells"]
    block.table["cells"][-2]["text"] = "↳"
    projection = table_projection(chunks_for(block, rows=5)[0], block)
    assert [4, 2] not in projection["covered_cells"]
    assert [4, 2] not in projection["empty_cells"]


def test_formula_missing_cache_and_saved_zero_are_distinct():
    block = cases()[3]["block"]
    projection = table_projection(chunks_for(block)[0], block)
    assert [(f["cell"], f["cache_status"], f["cached_value"]) for f in projection["formulas"]] == [
        ("B2", "missing", None), ("C2", "saved", 0)]


def test_row_group_projection_does_not_pull_unselected_rows_or_merge_anchor_text():
    block = cases()[4]["block"]
    chunk = chunks_for(block)[2]
    projection = table_projection(chunk, block)
    assert [row["row"] for row in projection["rows"]] == [1, 6, 7]
    assert "行1" not in json.dumps(projection, ensure_ascii=False)
    block = cases()[5]["block"]
    chunk = chunks_for(block)[1]
    projection = table_projection(chunk, block)
    assert projection["merges"][0]["anchor_visible"] is False
    assert "甲组" not in json.dumps(projection, ensure_ascii=False)


@pytest.mark.parametrize("change", ["text", "row_range", "foreign_doc", "foreign_version", "overlap", "overflow", "no_cells"])
def test_unverifiable_structure_falls_back_without_manufacturing_data(change):
    block = cases()[0]["block"]
    chunk = chunks_for(block)[0]
    if change == "text": chunk.text = chunk.text.replace("9", "99", 1)
    if change == "row_range": chunk.text += "\n行范围：第 3-4 行（表内行号，含表头 2 行）"
    if change == "foreign_doc": block.document_id = "foreign"
    if change == "foreign_version": block.parse_version_id = "foreign"
    if change == "overlap": block.table["cells"].append(cell(2, 1, "99"))
    if change == "overflow": chunk.chunk_type = "long_line"
    if change == "no_cells": block.table.pop("cells")
    assert table_projection(chunk, block)["status"] == "unavailable"
    assert table_projection(chunk, None)["reason"] == "missing_table_block"


def build(block, chunks, cap, tmp_path, *, overhead=0, **options):
    result = {"document_id": block.document_id, "index_id": "idx", "parse_version_id": block.parse_version_id,
        "results": [dict(chunk_id=c.id, score=1.0 - i / 100, **c.model_dump(exclude={"id"})) for i, c in enumerate(chunks)]}
    settings = Settings(data_dir=tmp_path, rag_context_max_chars=cap, rag_retrieval_k=20,
                        rag_context_k=20, rag_input_max_chars=cap + 10000, **options)
    return build_evidence(result, settings, expected_document_id=block.document_id,
                          payload_transform=task_evidence_payload, table_blocks=[block], table_prompt_overhead=overhead).pack


def test_auxiliary_metadata_never_displaces_raw_sources_and_counts_actual_json(tmp_path):
    block = cases()[4]["block"]
    chunks = chunks_for(block)
    unlimited = build(block, chunks, 20000, tmp_path)
    raw_entries = [{k: v for k, v in item.items() if k != "table_structure"} for item in unlimited.payload()]
    raw_length = len(json.dumps(task_evidence_payload(raw_entries), ensure_ascii=False, separators=(",", ":")))
    tight = build(block, chunks, raw_length, tmp_path)
    assert [i.chunk_id for i in tight.items] == [c.id for c in chunks]
    assert all(not item.table_projection_included for item in tight.items)
    assert all(item.table_projection_omitted == "table_projection_budget" for item in tight.items)
    assert not tight.truncated
    assert tight.context_chars == raw_length
    assert unlimited.context_chars == len(json.dumps(task_evidence_payload(unlimited.payload()), ensure_ascii=False, separators=(",", ":")))
    assert any(item.table_projection_included for item in unlimited.items)
    overhead_limited = build(block, chunks, unlimited.context_chars, tmp_path, overhead=20000)
    assert [i.chunk_id for i in overhead_limited.items] == [c.id for c in chunks]
    assert all(not item.table_projection_included for item in overhead_limited.items)


def table_environment(tmp_path):
    env = question_fixture(tmp_path)
    block = cases()[0]["block"]
    block.document_id, block.parse_version_id = "qdoc", "qv1"
    chunks = chunks_for(block, rows=5)
    with env.repository.connect() as db:
        db.execute("DELETE FROM chunks WHERE document_id='qdoc'")
    env.repository.finish_parse("qdoc", 1, chunks)
    with env.repository.connect() as db:
        db.execute("INSERT INTO blocks(id,document_id,parse_version_id,order_index,block_type,text,table_json,sheet_name,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)", (block.id, "qdoc", "qv1", 0, "table", block.text,
            json.dumps(block.table, ensure_ascii=False), block.sheet_name, now_iso()))
    # finish_parse复用该独立库的固定版本，仅为测试安装合成分块，不访问正式数据。
    env.rag.index.build("qdoc", rebuild=True)
    env.embedding.calls.clear()
    from tests.fixtures.rag_quality.claim_protocol import upgrade
    def response(system,user):
        body=upgrade({"status": "answered",
        "conclusion": [{"text": "退款金额为0。", "refs": [1], "context": [{"kind": "subject", "text": "退款", "ref": 1}]}],
        "explanation": [], "clarification_questions": [], "evidence_quotes": [{"ref": 1, "span": 1}],
        "answer_parts": [{"part_id": "p1", "state": "answered", "fact_indices": [0], "missing_detail": ""}]},
        {1: chunks[0].text})
        # 生产先声明表格行单元；合成桩引用实际选项，不沿用旧全文单元编号。
        reference=json.loads(user)['references'][0]
        body['conclusion'][0]['evidence']=[dict(ref=reference['ref'],unit=int(u)) for u in reference['quote_units']]
        return json.dumps(body,ensure_ascii=False)
    env.model.handler=response
    return env, block


def mutate_table(env):
    with env.repository.connect() as db:
        row = db.execute("SELECT id,table_json FROM blocks WHERE document_id='qdoc'").fetchone()
        table = json.loads(row["table_json"])
        table["cells"][-1]["text"] = "100"
        db.execute("UPDATE blocks SET table_json=? WHERE id=?", (json.dumps(table), row["id"]))


def test_real_task_persists_only_sent_structure_and_free_exports(tmp_path):
    env, block = table_environment(tmp_path)
    job, _ = submit(env, question="退款多少？")
    assert worker(env).run_job(job["id"])
    done = env.service.get_job(job["id"])
    assert done["status"] == "succeeded", done
    result = env.service.get_result(done["result_id"])
    metadata = result["answer"]["citations"][0]["table_structure"]
    assert metadata["rows"][-1]["values"] == ["退款", "", "0"]
    assert metadata["condition_scope_verified"] is False
    prompt = json.loads(env.model.calls[0][1])
    assert metadata == prompt["references"][0]["table_structure"]
    assert "# 本任务的表格行列辅助证据" in env.model.calls[0][0]
    assert "table_structure" in env.service.export(done["result_id"], "json")[0]
    assert len(env.embedding.calls) == len(env.model.calls) == 1


def test_non_table_task_keeps_base_prompt_budget_and_no_table_instructions(tmp_path):
    from app.rag_prompts import build_task_system_prompt
    env = question_fixture(tmp_path)
    job, _ = submit(env)
    assert worker(env).run_job(job["id"])
    system, user = env.model.calls[0]
    from tests.test_question_jobs import QUESTION
    assert system == build_task_system_prompt(include_table_structure=False,question=QUESTION)
    assert "# 本任务的表格行列辅助证据" not in system
    assert all("table_structure" not in r for r in json.loads(user)["references"])


@pytest.mark.parametrize("stage,expected_calls", [("before_query", 0), ("after_query", 1), ("before_generation", 1), ("after_generation", 2)])
def test_structure_mutation_stops_task_at_boundaries(tmp_path, stage, expected_calls):
    env, _ = table_environment(tmp_path)
    job, _ = submit(env, question="退款多少？")
    if stage == "before_query": mutate_table(env)
    if stage == "after_query":
        env.embedding.handler = lambda texts: (mutate_table(env) or [[1.0, 0.0] for _ in texts])
    if stage == "before_generation":
        original = env.service.store.save_checkpoint
        def save(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("role") == "evidence": mutate_table(env)
            return result
        # worker使用独立store实例，仍共享同一生产仓储方法。
        from app.question_jobs import QuestionJobStore
        from unittest.mock import patch
        with patch.object(QuestionJobStore, "save_checkpoint", side_effect=save):
            assert worker(env).run_job(job["id"])
    else:
        if stage == "after_generation":
            original = env.model.handler
            env.model.handler = lambda system, user: (mutate_table(env) or original(system, user))
        assert worker(env).run_job(job["id"])
    done = env.service.get_job(job["id"])
    assert done["status"] == "failed" and not done["result_id"], done
    assert done["requests_used"] == expected_calls
    assert len(env.model.calls) == (stage == "after_generation")


@pytest.mark.parametrize("target", ["source", "projection", "citation", "included", "version"])
def test_saved_answer_checkpoint_cannot_bypass_source_and_structure_validation(tmp_path, target):
    env, _ = table_environment(tmp_path)
    job, _ = submit(env, question="退款多少？")
    claimed = env.service.store.claim("fixture", 60, job_id=job["id"])
    snapshot = json.loads(claimed["snapshot_json"])
    evidence = {}
    def observer(stage, details):
        if stage == "evidence_fixed": evidence.update(details["diagnostics"])
        if stage == "query_completed":
            assert env.service.store.save_checkpoint(job['id'],claimed['lease_token'],role='retrieval',payload=details['search_result'],sequence_no=None)
    answer = env.rag.answer("qdoc", "退款多少？", snapshot=snapshot, observer=observer).answer.model_dump()
    if target == "source": evidence["selected"][0]["text"] += "伪造资料"
    if target == "projection": evidence["selected"][0]["table_projection"]["rows"][-1]["values"][-1] = "100"
    if target == "citation": answer["citations"][0]["table_structure"]["condition_scope_verified"] = True
    if target == "included": evidence["selected"][0]["table_projection_included"] = False
    if target == "version":
        snapshot["table_projection_version"] = "unknown"
        with env.repository.connect() as db:
            db.execute("UPDATE question_jobs SET snapshot_json=? WHERE id=?", (json.dumps(snapshot), job["id"]))
    assert env.service.store.save_checkpoint(job["id"], claimed["lease_token"], role="evidence", payload=evidence, sequence_no=None)
    with pytest.raises(ValueError): env.service.store.publish(job["id"], claimed["lease_token"], answer)
    with env.repository.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM question_results").fetchone()[0] == 0


def test_signature_includes_header_flags_footnotes_and_locations():
    block = cases()[0]["block"]
    original = table_structure_signature([block])
    block.table["cells"][0]["column_header"] = False
    assert table_structure_signature([block]) != original
    block = cases()[0]["block"]
    block.table["footnotes"].append("新范围")
    assert table_structure_signature([block]) != original
    block = cases()[0]["block"]
    block.sheet_name = "另一工作表"
    assert table_structure_signature([block]) != original


def test_large_projection_is_omitted_while_raw_chunk_remains(tmp_path):
    block = cases()[2]["block"]
    block.table["cells"][-1]["text"] = "长" * 5800
    chunks = chunks_for(block, max_chars=10000)
    pack = build(block, chunks, 15000, tmp_path)
    assert [i.chunk_id for i in pack.items] == [c.id for c in chunks]
    assert pack.items[0].table_projection_omitted == "table_projection_capacity"
    assert not pack.items[0].table_projection_included


def test_valid_saved_table_answer_survives_restart_without_generation(tmp_path):
    env, _ = table_environment(tmp_path)
    job, _ = submit(env, question="退款多少？")
    claimed = env.service.store.claim("old-worker", 60, job_id=job["id"])
    evidence = {}
    def observer(stage, details):
        if stage == "evidence_fixed": evidence.update(details["diagnostics"])
        if stage == "query_completed":
            assert env.service.store.save_checkpoint(job['id'],claimed['lease_token'],role='retrieval',payload=details['search_result'],sequence_no=None)
    answer = env.rag.answer("qdoc", "退款多少？", snapshot=json.loads(claimed["snapshot_json"]), observer=observer).answer.model_dump()
    assert env.service.store.save_checkpoint(job["id"], claimed["lease_token"], role="evidence", payload=evidence, sequence_no=None)
    assert env.service.store.save_checkpoint(job["id"], claimed["lease_token"], role="answer", payload=answer, sequence_no=None)
    counts = len(env.embedding.calls), len(env.model.calls)
    worker(env).process_job(claimed)
    done = env.service.get_job(job["id"])
    assert done["status"] == "succeeded", done
    assert (len(env.embedding.calls), len(env.model.calls)) == counts
    assert env.service.get_result(done["result_id"])["answer"]["citations"][0]["table_structure"] is not None
