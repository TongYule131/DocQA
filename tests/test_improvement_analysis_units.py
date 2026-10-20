"""U07合成大表回归：真实输入规划、校验和worker，模型边界使用离线桩。"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from app.analysis_jobs import AnalysisJobService
from app.analysis_prompts import EXTRACTION_PROMPT_VERSION, extraction_system_prompt
from app.analysis_sources import INPUT_PROTOCOL_VERSION, build_plan, build_units, plan_batches
from app.analysis_table_validation import validate_table_bindings
from app.analysis_validation import AnalysisOutputError, ValidatedItem, validate_extraction_batch
from app.analysis_worker import AnalysisWorker
from app.config import Settings
from app.repository import Repository, now_iso
from app.schemas import AnalysisJobRequest, Block, Chunk, ParseVersion, SourceLocation
from tests.test_analysis import _activate_version, _write_parsed_version, extraction_reply, seed_document


def table_block(rows=30, *, merged_body=False, huge_cell=False, missing_cache=False, headers=True):
    """合成双层列头；原表从C7开始，防止把局部行号冒充工作表行号。"""
    cells = [
        {"row": 0, "col": 0, "text": "地区", "column_header": headers, "row_span": 2},
        {"row": 0, "col": 1, "text": "年度", "column_header": headers, "row_span": 2},
        {"row": 0, "col": 2, "text": "收入", "column_header": headers, "col_span": 2},
        {"row": 0, "col": 4, "text": "成本", "column_header": headers, "col_span": 2},
    ]
    cells += [{"row": 1, "col": col, "text": label, "column_header": headers}
              for col, label in ((2, "主营业务"), (3, "其他业务"), (4, "主营业务"), (5, "其他业务"))]
    for index in range(rows):
        values = [f"对象{index + 1:03d}", "2024年", f"{1001 + index}.00", f"{701 + index}.50",
                  f"{501 + index}.00", "" if missing_cache and index == rows - 1 else f"{201 + index}.25"]
        if huge_cell and index == 0:
            values[0] = "单个业务描述" * 500
        for col, text in enumerate(values):
            if merged_body and col == 0 and index % 2:
                continue
            cells.append({"row": index + 2, "col": col, "text": text,
                          "row_span": 2 if merged_body and col == 0 and index % 2 == 0 else 1,
                          "row_header": col == 0})
    table = {"num_rows": rows + 2, "num_cols": 6, "cells": cells,
             "captions": ["合成地区经营表（单位：万元）"], "footnotes": ["全部金额为已审计口径。"],
             "sheet_name": "合成经营表", "cell_range": f"C7:H{rows + 8}"}
    if missing_cache:
        table["formulas"] = {f"H{rows + 8}": {"formula": "=F1-G1", "has_cache": False, "cached_value": None}}
    return Block(id="large-table", document_id="doc-1", parse_version_id="v-large", order_index=0,
        block_type="table", text="合成地区经营表", table=table, sheet_name="合成经营表",
        sources=[SourceLocation(format="xlsx", sheet_name="合成经营表", cell_range=table["cell_range"])])


def settings_for(tmp_path, **overrides):
    values = dict(data_dir=tmp_path, deepseek_api_key="test-generation-key",
                  analysis_batch_max_chars=1800, analysis_input_max_chars=20000,
                  analysis_reduce_max_chars=18000, analysis_max_requests=24)
    values.update(overrides)
    return Settings(**values)


def plan_for(block, settings, *, options=None):
    return build_plan(kind="extraction", blocks=[block], document_name="合成经营表.xlsx", version_id="v-large",
        settings=settings, prompt_version=EXTRACTION_PROMPT_VERSION,
        system_prompt_chars=len(extraction_system_prompt()), extraction_options=options)


def test_large_table_groups_cover_every_data_row_once_and_preserve_headers(tmp_path):
    block = table_block()
    original = deepcopy(block.model_dump())
    plan = plan_for(block, settings_for(tmp_path))
    assert plan.executable, plan.blocked_reason
    assert len(plan.units) > 1 and len(plan.batches) > 1
    assert INPUT_PROTOCOL_VERSION == "docqa-analysis-input-v3"
    row_ids = []
    all_facts = []
    for unit in plan.units:
        structure = unit.table_structure
        start, end = structure["row_group"]
        assert unit.unit_id == f"large-table::rows-{start}-{end}" and unit.block_id == "large-table"
        assert unit.sources == block.sources and unit.source_ids == [0]
        assert structure["row_mapping"] == [1, 2] + list(range(start, end + 1))
        assert len(unit.text) <= 600
        assert structure["column_paths"][2]["path"] == ["收入", "主营业务"]
        assert structure["column_paths"][4]["path"] == ["成本", "主营业务"]
        assert "单位：万元" in unit.text and "全部金额为已审计口径" in unit.text
        assert "地区 | 年度 | 收入 | ↳ | 成本 | ↳" in unit.text
        assert unit.payload()["table_structure"]["row_group"] == [start, end]
        row_ids += list(range(start, end + 1))
        all_facts += [(fact["original_row"], fact["col"], fact["value"]) for fact in structure["facts"]]
        assert structure["source_cell_ranges"] == ["C7:H8", f"C{start + 6}:H{end + 6}"]
    assert row_ids == list(range(3, 33))
    original_unit = build_units([block])[0][0]
    assert all_facts == [(fact["row"], fact["col"], fact["value"]) for fact in original_unit.table_structure["facts"]]
    assert len({unit.unit_id for unit in plan.units}) == len(plan.units)
    assert plan.total_chars == sum(len(unit.text) for unit in plan.units)
    assert all(chars <= plan_for(block, settings_for(tmp_path)).batch_message_chars[index]
               for index, chars in enumerate(plan.batch_message_chars))
    assert plan.fingerprint == plan_for(block, settings_for(tmp_path)).fingerprint
    assert block.model_dump() == original


def test_small_table_remains_exactly_one_unchanged_unit(tmp_path):
    block = table_block(rows=2)
    before = build_units([block])[0][0]
    plan = plan_for(block, settings_for(tmp_path, analysis_batch_max_chars=12000))
    assert plan.executable and len(plan.units) == 1
    assert plan.units[0] == before
    assert "row_group" not in plan.units[0].table_structure


def test_body_row_spans_are_never_cut_between_groups(tmp_path):
    plan = plan_for(table_block(rows=30, merged_body=True), settings_for(tmp_path))
    assert plan.executable and len(plan.units) > 1
    for unit in plan.units:
        start, end = unit.table_structure["row_group"]
        assert (start - 3) % 2 == 0 and (end - 2) % 2 == 0
        for cell in unit.table_structure["cells"]:
            assert cell["row"] + cell["row_span"] - 1 <= unit.table_structure["rows"]
        assert "↳" in unit.text


@pytest.mark.parametrize("options", [{"huge_cell": True}, {"headers": False}])
def test_unsafely_splittable_table_blocks_whole_plan_without_truncation(tmp_path, options):
    block = table_block(**options)
    original = build_units([block])[0][0]
    plan = plan_for(block, settings_for(tmp_path))
    assert not plan.executable and plan.blocked_reason == "unit_over_batch_budget"
    assert len(plan.units) == 1 and plan.units[0].text == original.text
    assert not plan.batches and plan.request_upper_bound == 0


def test_formula_missing_cache_stays_only_in_its_real_row_group(tmp_path):
    plan = plan_for(table_block(missing_cache=True), settings_for(tmp_path))
    assert plan.executable and len(plan.units) > 1
    containing = [unit for unit in plan.units if "缺失（不能当作 0）" in unit.text]
    assert len(containing) == 1 and containing[0] == plan.units[-1]
    assert "H38=" in containing[0].text
    assert not any(fact["col"] == 6 and fact["original_row"] == 32 for unit in plan.units for fact in unit.table_structure["facts"])


def test_unlocatable_formula_or_oversized_header_is_not_dropped(tmp_path):
    block = table_block(missing_cache=True)
    block.table["formulas"] = {"无法定位的公式": {"formula": "=A1", "has_cache": False}}
    plan = plan_for(block, settings_for(tmp_path))
    assert not plan.executable and "无法定位的公式" in plan.units[0].text
    block = table_block()
    block.table["footnotes"] = ["完整口径" * 800]
    plan = plan_for(block, settings_for(tmp_path))
    assert not plan.executable and "完整口径" * 800 in plan.units[0].text


def test_split_row_metadata_cannot_join_value_to_another_column_or_subject(tmp_path):
    plan = plan_for(table_block(), settings_for(tmp_path))
    unit = replace(plan.units[0], batch_local_id=1)
    good = ValidatedItem(item_id="i1", kind="data", content="对象001主营业务收入1001.00万元。",
        name="主营业务", value_text="1001.00", unit="万元", period="2024年", subject="对象001", scope="收入", refs=[1])
    assert validate_table_bindings([good], [unit])[0]["status"] == "verified"
    for invalid in (replace(good, scope="成本"), replace(good, subject="对象002"), replace(good, value_text="1001.000")):
        with pytest.raises(AnalysisOutputError, match="table_binding_mismatch"):
            validate_table_bindings([invalid], [unit])
    # 他组真实数值仍不在本次单元引述范围内，不能跨组借用引用。
    wrong = extraction_reply([{"kind": "data", "content": "对象030主营业务收入1030.00万元。",
        "name": "主营业务", "value_text": "1030.00", "subject": "对象030", "refs": [1]}], {"1": unit.text[:550]})
    with pytest.raises(AnalysisOutputError, match="field_not_in_quote"):
        validate_extraction_batch(wrong, units=[unit], batch_id="b1", max_items=15)


def test_real_ref_serialization_budget_includes_two_digit_ref(tmp_path):
    block = table_block(rows=2)
    unit = build_units([block])[0][0]
    units = [replace(unit, unit_id=f"copy-{index}", text="x", table_structure=None, heading_path=None) for index in range(15)]
    batches, missing = plan_batches(units, batch_max_chars=500)
    assert not missing
    for batch in batches:
        assert len(json.dumps([{**unit.payload(), "ref": index + 1} for index, unit in enumerate(batch)],
                              ensure_ascii=False, separators=(",", ":"))) <= 500


def seed_large_table(repository, block):
    seed_document(repository)
    version = ParseVersion(id="v-large", document_id="doc-1", task_id="ptask-doc-1", origin_hash="synthetic-large",
        parser_name="synthetic", config_summary="{}", result_schema_version="synthetic-large-v1",
        result_hash="synthetic-large-hash", quality_status="ok", block_count=1, page_count=1, chunk_count=1,
        created_at=now_iso())
    chunk = Chunk(id="large-chunk", document_id="doc-1", page=1, text=block.text,
        parse_version_id="v-large", block_id=block.id, order_index=0, sources=block.sources)
    _write_parsed_version(repository, version, [block], [chunk])
    _activate_version(repository, "doc-1", "v-large")


def test_production_worker_extracts_requested_first_and_last_rows_across_groups(tmp_path):
    settings = settings_for(tmp_path)
    repository = Repository(tmp_path / "docqa.db")
    seed_large_table(repository, table_block())
    service = AnalysisJobService(settings, repository)
    options = {"required_metrics": [
        {"name": "主营业务", "subject": "对象001", "period": "2024年", "scope": "收入"},
        {"name": "主营业务", "subject": "对象030", "period": "2024年", "scope": "收入"}], "include_highlights": False}
    outcome = service.build_plan("doc-1", "extraction", extraction_options=options)
    assert outcome.plan.executable and len(outcome.plan.batches) > 1
    response, _ = service.submit("doc-1", "extraction", AnalysisJobRequest(
        parse_version_id="v-large", plan_fingerprint=outcome.plan.fingerprint,
        idempotency_key="large-table-extract", extraction_options=options))

    class Model:
        calls = []
        def generate(self, system, user):
            self.calls.append(user)
            body = json.loads(user)
            items, quotes = [], {}
            for unit in body["units"]:
                for subject, value in (("对象001", "1001.00"), ("对象030", "1030.00")):
                    if subject not in unit["content"]:
                        continue
                    # 取包含必要表头和对应行的连续引述；末组通常仅有少量行。
                    quote = unit["content"]
                    if len(quote) > 600:
                        position = quote.index(subject)
                        if position < 500:
                            quote = quote[:quote.index("\n", position) if "\n" in quote[position:] else len(quote)]
                        else:
                            continue
                    items.append({"kind": "data", "content": f"已审计口径下，{subject}2024年主营业务收入为{value}万元。",
                        "name": "主营业务", "value_text": value, "unit": "万元", "period": "2024年",
                        "subject": subject, "scope": "收入", "refs": [unit["ref"]]})
                    quotes[str(unit["ref"])] = quote
            return extraction_reply(items, quotes)

    model = Model()
    assert AnalysisWorker(settings, repository, model=model).run_job(response.job.id)
    job = repository.get_analysis_job(response.job.id)
    assert job.status == "succeeded", (job.error_code, job.error_message)
    result = service.get_result(job.result_id)
    assert result.extraction.metric_coverage.status == "complete"
    assert {item.subject for item in result.extraction.items} == {"对象001", "对象030"}
    assert all(check["status"] == "verified" for check in result.extraction.table_checks)
    assert all(citation.block_id == "large-table" for citation in result.extraction.citations)
    assert len({citation.unit_id for citation in result.extraction.citations}) == 2
    assert result.coverage.processed_units == result.coverage.total_units == len(outcome.plan.units)
    assert job.requests_used == len(model.calls) == len(outcome.plan.batches)
