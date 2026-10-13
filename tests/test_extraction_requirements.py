"""混合提取的真实计划、幂等、worker、持久化和零隐式调用边界（合成数据）。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.analysis_jobs import AnalysisJobService
from app.analysis_validation import ValidatedItem
from app.analysis_worker import AnalysisWorker
from app.extraction_requirements import metric_coverage
from app.repository import Repository, TaskConflict
from app.schemas import AnalysisJobRequest, ExtractionOptions
from tests.test_analysis import StubModel, extraction_reply, fixture_settings, seed_document
from tests.test_analysis_api import Recorder, make_client, mock_transport


def setup_service(tmp_path, **overrides):
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret", **overrides)
    repo = Repository(tmp_path / "docqa.db")
    doc, version = seed_document(repo)
    return settings, repo, doc, version, AnalysisJobService(settings, repo)


def options(*names, include_highlights=True):
    return ExtractionOptions(required_metrics=[{"name": name} for name in names],
                             include_highlights=include_highlights)


def test_coverage_requires_one_item_to_match_all_criteria_and_keeps_missing_unresolved():
    items = [ValidatedItem(item_id="a", kind="data", content="合成事实", refs=[1],
                           name="营业收入", value_text="10", subject="华东", period="2024年"),
             ValidatedItem(item_id="b", kind="data", content="合成事实", refs=[2],
                           name="营业收入", value_text="20", subject="华北", period="2025年")]
    config = {"required_metrics": [
        {"name": "营业收入", "subject": "华东", "period": "2024 年"},
        {"name": "营业收入", "subject": "华东", "period": "2025年"},
        {"name": "收入"}, {"name": "不存在的指标"}]}
    result = metric_coverage(items, config)
    assert result["matched_count"] == 1 and result["status"] == "incomplete"
    assert result["metrics"][0]["item_ids"] == ["a"]
    assert all(check["status"] == "unresolved" for check in result["metrics"][1:])
    assert "不能判定原文不存在" in result["metrics"][1]["reason"]


def test_no_value_or_non_data_item_does_not_satisfy_numeric_metric():
    items = [SimpleNamespace(item_id="a", kind="conclusion", name="收入", value_text="10"),
             SimpleNamespace(item_id="b", kind="data", name="收入", value_text=None)]
    assert metric_coverage(items, options("收入"))["matched_count"] == 0
    assert metric_coverage(items)["status"] == "not_requested"


@pytest.mark.parametrize("payload", [
    {"required_metrics": [], "include_highlights": False},
    {"required_metrics": [{"name": "  "}]},
    {"required_metrics": [{"name": "指标", "subject": " "}]},
    {"required_metrics": [{"name": "指标", "unknown": "x"}]},
    {"required_metrics": [{"name": str(index)} for index in range(13)]},
    {"required_metrics": [{"name": "收入"}, {"name": "收 入"}]},
])
def test_invalid_or_ambiguous_options_rejected(payload):
    with pytest.raises(ValidationError):
        ExtractionOptions.model_validate(payload)


def test_plan_binds_options_and_counts_actual_request_characters(tmp_path):
    from app.extraction import build_batch_request
    from app.analysis_worker import _units_with_local_ids
    settings, repo, doc, _, service = setup_service(tmp_path)
    first = service.build_plan(doc, "extraction", extraction_options=options("营业收入"))
    second = service.build_plan(doc, "extraction", extraction_options=options("毛利率"))
    assert first.plan.fingerprint != second.plan.fingerprint
    for index, batch in enumerate(first.plan.batches):
        request = build_batch_request(units=_units_with_local_ids(batch), document_name=first.document.filename,
                                      version_id=first.version_id, batch_index=index + 1,
                                      batch_total=len(first.plan.batches),
                                      extraction_options=first.plan.extraction_options,
                                      max_items=settings.analysis_max_items_per_batch)
        assert first.plan.batch_message_chars[index] == len(request.system_prompt) + len(request.user_prompt)
        sent = json.loads(request.user_prompt)
        assert sent["extraction_options"]["required_metrics"][0]["name"] == "营业收入"
        assert sent["output_limits"]["max_items"] == settings.analysis_max_items_per_batch
    assert repo.list_analysis_jobs(doc) == []


def test_changed_options_cannot_reuse_idempotency_key_even_without_plan(tmp_path):
    _, repo, doc, _, service = setup_service(tmp_path)
    first, _ = service.submit(doc, "extraction", AnalysisJobRequest(
        idempotency_key="same", extraction_options=options("营业收入")))
    repeated, _ = service.submit(doc, "extraction", AnalysisJobRequest(
        idempotency_key="same", extraction_options=options("营业收入")))
    assert repeated.job.id == first.job.id
    with pytest.raises(TaskConflict):
        service.submit(doc, "extraction", AnalysisJobRequest(
            idempotency_key="same", extraction_options=options("毛利率")))
    with pytest.raises(TaskConflict):
        service.submit(doc, "extraction", AnalysisJobRequest(extraction_options=options("毛利率")))
    assert repo.analysis_calls_used(first.job.id) == 0


def test_plan_rejects_metric_changes_at_submission(tmp_path):
    _, repo, doc, version, service = setup_service(tmp_path)
    plan = service.build_plan(doc, "extraction", extraction_options=options("营业收入"))
    with pytest.raises(TaskConflict):
        service.submit(doc, "extraction", AnalysisJobRequest(
            parse_version_id=version, plan_fingerprint=plan.plan.fingerprint,
            extraction_options=options("毛利率")))
    assert repo.list_analysis_jobs(doc) == []


def test_worker_persists_partial_metric_coverage_without_claiming_fact_completeness(tmp_path):
    settings, repo, doc, _, service = setup_service(tmp_path)
    config = options("营业收入", "缺失指标")
    plan = service.build_plan(doc, "extraction", extraction_options=config).plan
    assert len(plan.batches) == 1
    response, _ = service.submit(doc, "extraction", AnalysisJobRequest(extraction_options=config))
    ref, unit = next((i, unit) for i, unit in enumerate(plan.batches[0], 1)
                     if "营业收入为 1.2 亿元" in unit.text)
    raw = extraction_reply([{"kind": "data", "content": "2024 年公司营业收入为 1.2 亿元。",
                             "name": "营业收入", "value_text": "1.2", "unit": "亿元",
                             "period": "2024 年", "subject": "公司", "refs": [ref]}],
                           {str(ref): unit.text})
    model = StubModel([raw])
    worker = AnalysisWorker(settings, repo, model=model)
    assert worker.run_job(response.job.id)
    result = AnalysisJobService(settings, Repository(tmp_path / "docqa.db")).get_result(f"res-{response.job.id}")
    assert result.coverage.complete is True
    assert result.extraction.metric_coverage.status == "incomplete"
    assert result.extraction.metric_coverage.matched_count == 1
    assert result.extraction.options == config
    assert result.requests_used == 1
    assert json.loads(model.calls[0][1])["extraction_options"] == config.model_dump()
    for fmt in ("json", "markdown"):
        body, _, _ = service.export(result.id, fmt)
        assert "缺失指标" in body
        assert "未确认" in body
    # 新指标计划不能复用旧的成功结果；旧结果和账本仍在。
    changed, _ = service.submit(doc, "extraction", AnalysisJobRequest(extraction_options=options("毛利率")))
    assert changed.job.id != response.job.id and not changed.reused
    assert service.get_result(result.id).extraction.metric_coverage.matched_count == 1
    assert repo.analysis_calls_used(response.job.id) == 1


def test_api_read_only_paths_and_summary_options_rejection_do_not_call_model(monkeypatch, tmp_path):
    settings, _, doc, _, _ = setup_service(tmp_path)
    recorder = Recorder([])
    mock_transport(monkeypatch, recorder)
    payload = {"extraction_options": options("营业收入").model_dump()}
    with make_client(settings) as client:
        plan = client.post(f"/api/documents/{doc}/analysis-plan?kind=extraction", json=payload)
        assert plan.status_code == 200
        assert plan.json()["extraction_options"] == payload["extraction_options"]
        assert client.post(f"/api/documents/{doc}/analysis-plan?kind=summary", json=payload).status_code == 422
        assert client.post(f"/api/documents/{doc}/summary", json=payload).status_code == 422
        job = client.post(f"/api/documents/{doc}/extract", json={**payload,
                          "plan_fingerprint": plan.json()["plan_fingerprint"]})
        assert job.status_code == 202
        assert client.get(f"/api/analysis-jobs/{job.json()['job']['id']}").status_code == 200
        assert client.post(f"/api/documents/{doc}/analysis-plan", json={"system": "x"}).status_code == 422
    assert recorder.requests == []


def test_no_highlights_rejects_unrequested_valid_fact_without_silently_removing_it(tmp_path):
    settings, repo, doc, _, service = setup_service(tmp_path)
    config = options("营业收入", include_highlights=False)
    plan = service.build_plan(doc, "extraction", extraction_options=config).plan
    response, _ = service.submit(doc, "extraction", AnalysisJobRequest(extraction_options=config))
    ref, unit = next((i, unit) for i, unit in enumerate(plan.batches[0], 1)
                     if "营业收入为 1.2 亿元" in unit.text)
    raw = extraction_reply([
        {"kind": "data", "content": "营业收入为 1.2 亿元", "name": "营业收入",
         "value_text": "1.2", "refs": [ref]},
        {"kind": "data", "content": "同比增长15.5%", "name": "同比增长",
         "value_text": "15.5%", "refs": [ref]}], {str(ref): unit.text})
    model = StubModel([raw])
    assert AnalysisWorker(settings, repo, model=model).run_job(response.job.id)
    final = repo.get_analysis_job(response.job.id)
    assert final.status == "failed" and final.error_code == "output_invalid:unrequested_item"
    assert final.result_id is None and repo.analysis_calls_used(final.id) == 1
    assert len(model.calls) == 1


def test_required_metric_can_select_parent_header_and_scope_selects_child():
    from app.analysis_prompts import extraction_system_prompt
    item = ValidatedItem(item_id="a", kind="data", content="合成事实", refs=[1],
                         name="营业收入", scope="其他业务", subject="华东", value_text="23")
    config = {"required_metrics": [{"name": "营业收入", "scope": "其他业务", "subject": "华东"}]}
    assert metric_coverage([item], config)["status"] == "complete"
    assert "指定指标的 name 若对应某一级原文表头" in extraction_system_prompt()


def test_blocked_plan_still_displays_requested_metrics(tmp_path):
    _, _, doc, _, service = setup_service(tmp_path, analysis_batch_max_chars=10)
    plan = service.build_plan(doc, "extraction", extraction_options=options("营业收入"))
    response = service.plan_response(plan, "extraction")
    assert not response.executable
    assert response.extraction_options == options("营业收入")


def test_multi_batch_metric_matched_elsewhere_keeps_other_batch_limitation_local(tmp_path):
    settings, repo, doc, _, service = setup_service(tmp_path, analysis_batch_max_chars=900)
    config = options("营业收入")
    plan = service.build_plan(doc, "extraction", extraction_options=config).plan
    assert len(plan.batches) >= 2
    response, _ = service.submit(doc, "extraction", AnalysisJobRequest(extraction_options=config))
    replies = []
    note = "本批营业收入尚未确认"
    for batch in plan.batches:
        income = next(((i, unit) for i, unit in enumerate(batch, 1)
                       if "营业收入为 1.2 亿元" in unit.text), None)
        if income:
            ref, unit = income
            replies.append(extraction_reply([{"kind": "data", "content": "营业收入为1.2亿元",
                                             "name": "营业收入", "value_text": "1.2", "refs": [ref]}],
                                            {str(ref): unit.text}))
        else:
            replies.append(extraction_reply([], {}, limitations=[note]))
    assert AnalysisWorker(settings, repo, model=StubModel(replies)).run_job(response.job.id)
    result = service.get_result(f"res-{response.job.id}")
    assert result.extraction.metric_coverage.status == "complete"
    assert note not in result.limitations
    assert any("批局部限制：" + note in text for text in result.limitations)
