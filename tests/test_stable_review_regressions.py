"""独立验收反例：仅合成输入和模型桩，不读取真实配置或发外部请求。"""
import json
from dataclasses import replace

import pytest

from app.analysis_jobs import AnalysisJobService
from app.analysis_worker import AnalysisWorker
from app.analysis_sources import batch_message_chars, render_table_text
from app.analysis_prompts import summary_system_prompt
from app.repository import Repository, TaskConflict
from app.schemas import AnalysisJobRequest, Block
from app.summarization import build_batch_request, build_reduce_request
from tests.test_analysis import (
    fixture_settings, seed_document, StubModel, _submit, summary_reply, extraction_reply,
)


def setup_job(tmp_path, kind="summary", **settings_overrides):
    settings = fixture_settings(tmp_path, deepseek_api_key="offline-fake", **settings_overrides)
    repo = Repository(tmp_path / "docqa.db")
    doc, version = seed_document(repo)
    service = AnalysisJobService(settings, repo)
    outcome = service.build_plan(doc, kind)
    response, _ = _submit(service, doc, kind, idempotency_key="review-fixed-key")
    return settings, repo, service, outcome, response.job


def test_direct_summary_preserves_exception_reference(tmp_path):
    settings, repo, service, outcome, job = setup_job(tmp_path)
    first, second = outcome.plan.batches[0][1:3]
    model = StubModel([summary_reply("经营情况", [{"text": first.text, "refs": [2]}],
                                    [{"text": second.text, "refs": [3]}],
                                    {"2": first.text, "3": second.text})])
    AnalysisWorker(settings, repo, model=model).run_job(job.id)
    result = service.get_result(repo.get_analysis_job(job.id).result_id).summary
    quotes = {c.reference_id: c.quote for c in result.citations}
    assert quotes[result.exceptions[0].refs[0]] == second.text
    assert result.citations[0].source_index == 0


@pytest.mark.parametrize("kind", ["summary", "extraction"])
def test_cancel_during_last_call_never_publishes(tmp_path, kind):
    settings, repo, service, outcome, job = setup_job(tmp_path, kind)
    text = outcome.plan.batches[0][0].text
    raw = (summary_reply("概述", [{"text": "要点", "refs": [1]}], [], {"1": text})
           if kind == "summary" else extraction_reply([], {}))
    model = StubModel([raw], on_call=lambda *_: service.cancel_job(job.id))
    AnalysisWorker(settings, repo, model=model).run_job(job.id)
    current = repo.get_analysis_job(job.id)
    assert current.status == "cancelled"
    assert current.result_id is None and current.requests_used == 1


def test_cancel_is_checked_atomically_before_budget(tmp_path):
    _, repo, service, _, job = setup_job(tmp_path)
    claimed = repo.claim_next_analysis_job("review", 60, job_id=job.id)
    service.cancel_job(job.id)
    assert repo.claim_analysis_budget(job.id, claimed.lease_token, role="batch", step_id="x") is None
    assert repo.analysis_calls_used(job.id) == 0


def test_explicit_retry_acknowledges_uncertain_without_erasing_cost(tmp_path):
    settings, repo, service, outcome, job = setup_job(tmp_path, "extraction")
    # 原实现把上限压成计划次数；保留余量以单独复现不确定记录阻塞明确重试。
    with repo.connect() as db:
        db.execute("UPDATE analysis_jobs SET max_requests=8 WHERE id=?", (job.id,))
    AnalysisWorker(settings, repo, model=StubModel([RuntimeError("offline interruption")])).run_job(job.id)
    assert repo.get_analysis_job(job.id).status == "needs_attention"
    service.retry_job(job.id)
    model = StubModel([extraction_reply([], {})])
    AnalysisWorker(settings, repo, model=model).run_job(job.id)
    assert repo.get_analysis_job(job.id).status == "succeeded"
    calls = repo.analysis_calls(job.id)
    assert len(calls) == 2 and calls[0].status == "uncertain"
    assert repo.get_analysis_job(job.id).requests_used == 2


def test_task_budget_retains_explicit_retry_allowance(tmp_path):
    settings, repo, service, _, job = setup_job(tmp_path, "extraction")
    assert job.request_upper_bound == 1
    assert job.max_requests == settings.analysis_max_requests
    AnalysisWorker(settings, repo, model=StubModel(["broken JSON"])).run_job(job.id)
    service.retry_job(job.id)
    AnalysisWorker(settings, repo, model=StubModel([extraction_reply([], {})])).run_job(job.id)
    assert repo.get_analysis_job(job.id).requests_used == 2


def test_same_key_different_plan_is_conflict(tmp_path):
    _, _, service, outcome, job = setup_job(tmp_path)
    with pytest.raises(TaskConflict):
        service.submit(job.document_id, "summary", AnalysisJobRequest(
            idempotency_key=job.idempotency_key, parse_version_id=job.parse_version_id,
            plan_fingerprint="different-plan"))


def test_concurrent_repository_key_conflict_is_checked(tmp_path):
    _, repo, _, _, job = setup_job(tmp_path)
    other = job.model_copy(update={"id": "other", "kind": "extraction"})
    with pytest.raises(TaskConflict):
        repo.create_analysis_job(other, plan={}, input_hash="x", request_fingerprint="different")


def test_changed_model_does_not_execute_old_job(tmp_path):
    settings, repo, _, _, job = setup_job(tmp_path, "extraction")
    changed = replace(settings, deepseek_model="different-model")
    model = StubModel([extraction_reply([], {})])
    AnalysisWorker(changed, repo, model=model).run_job(job.id)
    assert model.calls == []
    assert repo.get_analysis_job(job.id).status == "failed"


def test_retry_cannot_bypass_total_item_limit(tmp_path):
    settings, repo, service, outcome, job = setup_job(
        tmp_path, "extraction", analysis_batch_max_chars=420,
        analysis_max_requests=12, analysis_max_items_per_batch=1, analysis_max_items_total=1)
    with repo.connect() as db:
        db.execute("UPDATE analysis_jobs SET max_requests=12 WHERE id=?", (job.id,))
    replies = [extraction_reply([{"kind": "data", "content": f"合成条目{i}", "refs": [1]}],
                                {"1": batch[0].text}) for i, batch in enumerate(outcome.plan.batches)]
    model = StubModel(replies)
    AnalysisWorker(settings, repo, model=model).run_job(job.id)
    assert repo.get_analysis_job(job.id).status == "failed"
    service.retry_job(job.id)
    retry_model = StubModel(replies[2:])
    AnalysisWorker(settings, repo, model=retry_model).run_job(job.id)
    assert repo.get_analysis_job(job.id).status == "failed"
    assert retry_model.calls == []
    assert service.list_results(job.document_id, "extraction") == []


def test_actual_batch_message_size_matches_plan(tmp_path):
    _, _, _, outcome, _ = setup_job(tmp_path)
    from app.analysis_worker import _units_with_local_ids
    units = _units_with_local_ids(outcome.plan.batches[0])
    request = build_batch_request(units=units, document_name="合成样本-经营说明.txt",
                                 version_id=outcome.version_id, batch_index=1, batch_total=1)
    assert outcome.plan.batch_message_chars[0] == len(request.system_prompt) + len(request.user_prompt)


def test_reduce_sees_all_full_original_evidence():
    quotes = ["原文" * 90 + "但未成年人不适用。", "仅限2026年生效。"]
    entries = [{"item_id": "b1-p1", "kind": "point", "text": "条款及限制", "batch_id": "b1",
                "quote_preview": quotes[0][:80],
                "original_refs": [{"block_id": f"b{i}", "quote": q, "source_index": 0}
                                  for i, q in enumerate(quotes)]}]
    request = build_reduce_request(document_name="合成", version_id="v1", batch_total=2, entries=entries)
    for quote in quotes:
        assert quote in request.user_prompt


def test_reduce_budget_rejected_before_any_request(tmp_path):
    settings = fixture_settings(tmp_path, deepseek_api_key="offline-fake",
                                analysis_batch_max_chars=420, analysis_reduce_max_chars=100)
    repo = Repository(tmp_path / "docqa.db")
    doc, _ = seed_document(repo)
    outcome = AnalysisJobService(settings, repo).build_plan(doc, "summary")
    assert outcome.plan.executable is False
    assert "reduce" in outcome.plan.blocked_reason


def test_same_values_in_adjacent_table_cells_are_not_merged():
    block = Block(id="b", document_id="d", parse_version_id="v", order_index=0,
                  block_type="table", text="", table={"cells": [
                      {"row": 0, "col": 0, "text": "2024"}, {"row": 0, "col": 1, "text": "2025"},
                      {"row": 1, "col": 0, "text": "100"}, {"row": 1, "col": 1, "text": "100"}]})
    assert "100 | 100" in render_table_text(block)


def test_unclassified_exception_does_not_leak_raw_text(tmp_path, caplog):
    settings, repo, _, _, job = setup_job(tmp_path, "extraction")
    secret = "REVIEW_FAKE_CREDENTIAL_AND_RAW_BODY"
    AnalysisWorker(settings, repo, model=StubModel([RuntimeError(secret)])).run_job(job.id)
    assert secret not in caplog.text


def test_markdown_export_neutralizes_remote_images(tmp_path):
    settings, repo, service, outcome, job = setup_job(tmp_path, "extraction")
    hostile = "![pixel](https://example.invalid/tracker)"
    # 限制说明允许普通方括号；事实内容的自写引用规则不应为测试而放宽。
    model = StubModel([extraction_reply([], {}, limitations=[hostile])])
    AnalysisWorker(settings, repo, model=model).run_job(job.id)
    body, _, _ = service.export(repo.get_analysis_job(job.id).result_id, "markdown")
    assert hostile not in body


def test_summary_cannot_publish_calculated_growth_absent_from_quotes(tmp_path):
    settings, repo, _, outcome, job = setup_job(tmp_path)
    source = outcome.plan.batches[0][1].text
    model = StubModel([summary_reply("经营情况", [{"text": "隐含增速约16.7%", "refs": [2]}],
                                    [], {"2": source})])
    AnalysisWorker(settings, repo, model=model).run_job(job.id)
    result = repo.get_analysis_job(job.id)
    assert result.status == "failed" and result.error_code == "output_invalid:number_not_in_quote"
    assert result.result_id is None


def test_reused_task_keeps_second_key_and_regenerate_conflict(tmp_path):
    settings, repo, service, _, job = setup_job(tmp_path, "extraction")
    same, _ = _submit(service, "doc-1", "extraction", idempotency_key="second-key")
    assert same.job.id == job.id
    with pytest.raises(TaskConflict):
        _submit(service, "doc-1", "summary", idempotency_key="second-key")
    AnalysisWorker(settings, repo, model=StubModel([extraction_reply([], {})])).run_job(job.id)
    cached, _ = _submit(service, "doc-1", "extraction", idempotency_key="cache-key")
    assert cached.job.id == job.id
    with pytest.raises(TaskConflict):
        _submit(service, "doc-1", "extraction", idempotency_key="cache-key", regenerate=True)
    with pytest.raises(TaskConflict):
        _submit(service, "doc-1", "summary", idempotency_key="cache-key")


def test_summary_allows_explicit_missing_year_without_inventing_measure(tmp_path):
    from app.analysis_validation import validate_summary_batch
    _, _, _, outcome, _ = setup_job(tmp_path)
    units = [replace(unit, batch_local_id=index + 1) for index, unit in enumerate(outcome.plan.batches[0])]
    quote = units[6].text
    raw = summary_reply("表格", [{"text": "表内未列示2024年数据", "refs": [7]}], [], {"7": quote})
    assert validate_summary_batch(raw, units=units, batch_id="b1").main_points


def test_v4_migration_backfills_keys_without_changing_jobs(tmp_path):
    from app import migrations
    _, repo, _, _, job = setup_job(tmp_path)
    with repo.connect() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM analysis_jobs")]
        db.execute("DROP TABLE analysis_request_keys")
        db.execute("DELETE FROM schema_migrations WHERE version=4")
    def fail_migration():
        raise RuntimeError("合成 v4 迁移故障")
    migrations.MigrationHook.register(4, fail_migration)
    try:
        with pytest.raises(migrations.MigrationError):
            repo.initialize()
    finally:
        migrations.MigrationHook.clear()
    with repo.connect() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='analysis_request_keys'").fetchone() is None
        assert before == [tuple(row) for row in db.execute("SELECT * FROM analysis_jobs")]
    migration = repo.initialize()
    assert migration["applied"] == [4] and migration["backup"]
    with repo.connect() as db:
        assert before == [tuple(row) for row in db.execute("SELECT * FROM analysis_jobs")]
    assert repo.find_analysis_job_by_idempotency("doc-1", "review-fixed-key").id == job.id


def test_simultaneous_submit_and_claim_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    settings = fixture_settings(tmp_path, deepseek_api_key="offline-fake")
    repo = Repository(tmp_path / "docqa.db")
    seed_document(repo)
    gate = Barrier(2)
    def submit():
        gate.wait()
        return _submit(AnalysisJobService(settings, repo), "doc-1", "extraction", idempotency_key="race")[0].job.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        ids = [future.result() for future in futures]
    assert ids[0] == ids[1] and len(repo.list_analysis_jobs("doc-1")) == 1
    gate = Barrier(2)
    def claim(worker):
        gate.wait()
        return repo.claim_next_analysis_job(worker, 60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, str(index)) for index in range(2)]
        claims = [future.result() for future in futures]
    assert sum(item is not None for item in claims) == 1


def test_analysis_budget_configuration_is_enforced(tmp_path, monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("DOCQA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("DOCQA_ANALYSIS_MAX_REQUESTS", "3")
    assert Settings.from_env().analysis_max_requests == 3
    monkeypatch.setenv("DOCQA_ANALYSIS_MAX_REQUESTS", "0")
    with pytest.raises(ValueError, match="DOCQA_ANALYSIS_MAX_REQUESTS"):
        Settings.from_env()
    for overrides in ({"analysis_max_requests": 51}, {"analysis_batch_max_chars": 0},
                      {"analysis_input_max_chars": 10}):
        with pytest.raises(ValueError):
            fixture_settings(tmp_path, **overrides)
