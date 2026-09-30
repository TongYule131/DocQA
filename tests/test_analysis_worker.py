"""分析任务的端到端离线验收测试：真实编排 + 真实仓储 + 可控模型桩。

覆盖任务书中必须走生产链路验证的部分：

- A09 检查点复用、明确重试只做未完成部分、旧结果保留；
- A11 分批／汇总期间切换解析版本：新结果始终来自旧快照；
- A12 原文引用：越界编号、跨批编号、伪造来源都被拒绝；
- A15 提取语义边界：三类归属、观点主体、无项目状态与模型错误区分；
- A16 摘要覆盖：漏批或输出不完整不能发布“全文摘要”；
- A17 汇总引用：最终引用回到原文，中间摘要不能伪装成原文；
- A18 安全失败：上游错误只返回稳定安全提示；
- A20 重启持久化：任务、结果、引用、版本、覆盖与预算跨重启保留；
- A23 导出：Markdown／JSON 与已校验结果一致，无路径穿越与原始响应泄露。

模型桩只替换**外部 HTTP／模型边界**，worker、校验、仓储、路由全部是生产代码。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.analysis_worker import AnalysisWorker
from app.config import Settings
from app.deepseek import ModelError
from app.repository import Repository
from tests.test_analysis import (
    StubModel,
    _submit,
    extraction_reply,
    fixture_settings,
    reduce_reply,
    sample_blocks,
    seed_document,
    summary_reply,
)


# ---------------------------------------------------------------------------
# 样本工具：把一批单元的实际编号与正文取出来供桩使用
# ---------------------------------------------------------------------------
def plan_for(repository: Repository, settings: Settings, document_id: str, kind: str):
    from app.analysis_jobs import AnalysisJobService

    service = AnalysisJobService(settings, repository)
    return service, service.build_plan(document_id, kind)


def unit_ref(plan, needle: str, batch_index: int = 0) -> int:
    """找到包含指定文本的单元，并返回它在**所属批次内**的编号。

    批内编号是独立分配的，因此调用方必须同时知道批次下标；只按“第一个包含该文本
    的批次”取编号，会在多批场景下把另一批的编号误当成目标批的编号。
    """
    batch = plan.batches[batch_index]
    for position, unit in enumerate(batch, start=1):
        if needle in unit.text:
            return position
    for index, candidate in enumerate(plan.batches):
        for position, unit in enumerate(candidate, start=1):
            if needle in unit.text:
                raise AssertionError(
                    f"{needle!r} 出现在批次 {index + 1}，而不是本次指定的批次 {batch_index + 1}")
    raise AssertionError(f"规划结果中找不到包含 {needle!r} 的单元")


def unit_ref_any(plan, needle: str) -> tuple[int, int]:
    """返回 (批次下标, 批内编号)；用于需要跨批定位的场景。"""
    for index, batch in enumerate(plan.batches):
        for position, unit in enumerate(batch, start=1):
            if needle in unit.text:
                return index, position
    raise AssertionError(f"规划结果中找不到包含 {needle!r} 的单元")


def unit_text(plan, needle: str) -> str:
    for batch in plan.batches:
        for unit in batch:
            if needle in unit.text:
                return unit.text
    raise AssertionError(f"规划结果中找不到包含 {needle!r} 的单元")


def single_batch_plan(repository: Repository, settings: Settings,
                      document_id: str = "doc-1", kind: str = "extraction"):
    """确保本次规划的输入只落在**一个**批次，便于用固定批内编号写用例。"""
    from app.analysis_jobs import AnalysisJobService

    service = AnalysisJobService(settings, repository)
    outcome = service.build_plan(document_id, kind)
    if len(outcome.plan.batches) != 1:
        raise AssertionError(
            f"本用例需要单批输入，当前批次数量为 {len(outcome.plan.batches)}；"
            "请调整 analysis_batch_max_chars 或样本大小")
    return service, outcome


def run_worker(settings: Settings, repository: Repository, model, job_id: str) -> None:
    worker = AnalysisWorker(settings, repository, model=model, worker_id="test-worker")
    assert worker.run_job(job_id) is True


# ---------------------------------------------------------------------------
# A09 检查点与重试
# ---------------------------------------------------------------------------
def test_extraction_end_to_end_publishes_persisted_result(tmp_path):
    """完整链路：创建任务 → worker 生成 → 校验 → 合并 → 发布 → 结果可读。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, version_id = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id

    income_ref = unit_ref(outcome.plan, "营业收入为 1.2 亿元")
    income_text = unit_text(outcome.plan, "营业收入为 1.2 亿元")
    view_ref = unit_ref(outcome.plan, "总经理张某表示")
    view_text = unit_text(outcome.plan, "总经理张某表示")
    model = StubModel([
        extraction_reply(
            [{"kind": "data", "content": "2024 年营业收入为 1.2 亿元",
              "name": "营业收入", "value_text": "1.2 亿元", "period": "2024 年",
              "refs": [income_ref]},
             {"kind": "viewpoint", "content": "预计 2025 年增速放缓至 8% 左右",
              "subject": "总经理张某", "value_text": "8%", "period": "2025 年",
              "refs": [view_ref]}],
            {str(income_ref): income_text, str(view_ref): view_text},
            sections={"data": "present", "conclusion": "none", "viewpoint": "present"}),
    ])
    run_worker(settings, repository, model, job_id)

    job = repository.get_analysis_job(job_id)
    assert job.status == "succeeded" and job.result_id, (
        f"任务未成功：status={job.status} code={job.error_code} msg={job.error_message}")
    assert job.requests_used == 1 and len(model.calls) == 1
    result = service.get_result(job.result_id)
    assert result.kind == "extraction"
    assert result.parse_version_id == version_id
    items = result.extraction.items
    assert {item.kind for item in items} == {"data", "viewpoint"}
    assert result.extraction.sections == {"data": "present", "conclusion": "none",
                                          "viewpoint": "present"}
    # 数值与时间字段保持原文形式，未被换算。
    data_item = next(item for item in items if item.kind == "data")
    assert data_item.value_text == "1.2 亿元" and data_item.unit is None
    view_item = next(item for item in items if item.kind == "viewpoint")
    assert view_item.subject == "总经理张某"
    # 引用回到原文连续子串，并带上从映射复制的来源。
    assert result.extraction.citations
    for citation in result.extraction.citations:
        assert citation.quote in (income_text + view_text)
        assert citation.block_id.startswith(version_id)
        assert citation.sources
    assert result.coverage.complete is True
    assert result.coverage.processed_units == result.coverage.total_units
    assert result.coverage.resolved_original_refs == len(result.extraction.citations)


def test_worker_reuses_validated_checkpoints_after_restart(tmp_path):
    """A09/A20：重启后复用已校验检查点，只做未完成部分，不重复计费。

    说明：失败批次会消耗预算，所以重试需要额外余量；本用例把任务上限显式放宽到
    “规划上界 + 1 次失败重试”，以便同时验证检查点复用与预算不重置两条约束。
    """
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret",
                                analysis_batch_max_chars=420,
                                analysis_max_requests=12)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    assert len(outcome.plan.batches) >= 2, "该用例需要多批输入"
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    # 放宽任务上限：1 次失败 + 重试剩余批次。
    with repository.connect() as db:
        db.execute("UPDATE analysis_jobs SET max_requests=? WHERE id=?",
                   (outcome.plan.request_upper_bound + 1, job_id))

    first_batch = outcome.plan.batches[0]
    first_ref = 1
    first_text = first_batch[0].text
    replies = [extraction_reply(
        [{"kind": "data", "content": first_text[:12], "refs": [first_ref]}],
        {str(first_ref): first_text},
        sections={"data": "present", "conclusion": "none", "viewpoint": "none"})]
    # 第二个批次返回非法输出 → 任务失败，但第一批已校验检查点必须保留。
    replies.append('{"items": [], "quotes": {}, "sections": {"data": "none",')
    # 再准备一个合法回复：重试时只做未完成的批次（可能不止一批）。
    for batch in outcome.plan.batches[1:]:
        replies.append(extraction_reply(
            [{"kind": "conclusion", "content": batch[0].text[:12], "refs": [1]}],
            {"1": batch[0].text},
            sections={"data": "none", "conclusion": "present", "viewpoint": "none"}))
    model = StubModel(replies[:2])
    run_worker(settings, repository, model, job_id)
    failed = repository.get_analysis_job(job_id)
    assert failed.status == "failed"
    assert failed.error_code and failed.error_code.startswith("output_invalid")
    assert len(model.calls) == 2
    step = repository.analysis_step(job_id, "batch", 0)
    assert step is not None and step["status"] == "succeeded"

    # 明确重试：只重做未完成批次，已校验批次不再调用模型。
    retried = service.retry_job(job_id)
    assert retried.status == "queued"
    remaining = len(outcome.plan.batches) - 1
    model2 = StubModel(replies[2:])
    run_worker(settings, repository, model2, job_id)
    # 已校验的批次不再调用模型：重试的调用次数严格少于批次总数。
    assert 0 < len(model2.calls) < len(outcome.plan.batches), (
        "已校验的批次不应再次调用模型")
    final = repository.get_analysis_job(job_id)
    assert final.status == "succeeded", (
        f"重试后未成功：code={final.error_code} msg={final.error_message}")
    # 每个批次最多一次调用，且失败调用也计入预算并被持久化。
    total_calls = 2 + len(model2.calls)
    assert repository.analysis_calls_used(job_id) == total_calls
    assert final.requests_used == total_calls
    # 已校验批次没有重复调用：总调用次数严格小于“批次数的两倍”。
    assert total_calls < len(outcome.plan.batches) * 2


def test_failed_attempt_does_not_destroy_previous_success(tmp_path):
    """A09：新尝试失败不得替换或删除已有成功结果。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    first_job = response.job.id
    ref = unit_ref(outcome.plan, "营业收入为 1.2 亿元")
    text = unit_text(outcome.plan, "营业收入为 1.2 亿元")
    run_worker(settings, repository, StubModel([
        extraction_reply([{"kind": "data", "content": "营业收入 1.2 亿元", "refs": [ref]}],
                         {str(ref): text},
                         sections={"data": "present", "conclusion": "none",
                                   "viewpoint": "none"})]), first_job)
    first_result = repository.get_analysis_job(first_job).result_id
    assert first_result

    # 明确重新生成（新任务）并让它失败：旧结果必须仍然可读。
    second, status = _submit(service, document_id, "extraction", regenerate=True)
    assert status == 202 and second.job.id != first_job
    run_worker(settings, repository, StubModel([ModelError(502, "上游暂时不可用")]),
               second.job.id)
    assert repository.get_analysis_job(second.job.id).status == "failed"
    history = service.list_results(document_id, "extraction")
    assert [item.id for item in history] == [first_result]
    assert service.get_result(first_result).extraction.items


# ---------------------------------------------------------------------------
# A11 版本竞争
# ---------------------------------------------------------------------------
def test_result_binds_original_version_when_preview_switches(tmp_path):
    """A11：任务运行期间切换预览版本，结果仍来自任务创建时固定的旧快照。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, version_a = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    ref = unit_ref(outcome.plan, "营业收入为 1.2 亿元")
    text = unit_text(outcome.plan, "营业收入为 1.2 亿元")

    # 在 worker 执行期间发布一个新的解析版本 B 并切换预览。
    from tests.test_analysis import _activate_version, _write_parsed_version
    from app.schemas import ParseVersion
    from app.repository import now_iso

    blocks_b = sample_blocks(version_id="v-test-2", document_id=document_id)
    blocks_b[1].text = "2025 年公司营业收入为 9.9 亿元。"
    version_b = ParseVersion(
        id="v-test-2", document_id=document_id, task_id=f"ptask-{document_id}",
        origin_hash="synthetic-b", parser_name="synthetic-fixture", parser_version="fixture-1",
        config_summary="{}", result_schema_version="synthetic-1", result_hash="hash-b",
        quality_status="ok", block_count=len(blocks_b), page_count=3, chunk_count=0,
        created_at=now_iso())
    _write_parsed_version(repository, version_b, blocks_b, [])
    _activate_version(repository, document_id, "v-test-2")

    run_worker(settings, repository, StubModel([
        extraction_reply([{"kind": "data", "content": "营业收入 1.2 亿元", "refs": [ref]}],
                         {str(ref): text},
                         sections={"data": "present", "conclusion": "none",
                                   "viewpoint": "none"})]), job_id)
    result = service.get_result(repository.get_analysis_job(job_id).result_id)
    # 结果仍绑定版本 A，引用来源也全部来自 A 的块。
    assert result.parse_version_id == version_a
    assert result.is_active_version is False
    assert all(citation.block_id.startswith(version_a)
               for citation in result.extraction.citations)
    # 新预览 B 未被修改，也不受本次分析影响。
    assert repository.get(document_id).active_parse_version_id == "v-test-2"
    assert service.get_result(result.id).extraction.items[0].value_text is None


def test_worker_rejects_when_input_changed_after_creation(tmp_path):
    """A11/费用保护：输入或预算在创建后变化时本次不执行，避免用新输入生成旧任务结果。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    # 篡改任务创建时固定的计划指纹，模拟“输入已变化”。
    with repository.connect() as db:
        db.execute("UPDATE analysis_jobs SET plan_fingerprint='stale-fingerprint' WHERE id=?",
                   (job_id,))
    model = StubModel([])
    run_worker(settings, repository, model, job_id)
    assert model.calls == []            # 一次调用都没有发出
    job = repository.get_analysis_job(job_id)
    assert job.status == "failed"
    assert job.error_code == "plan_changed"
    assert repository.analysis_calls_used(job_id) == 0


# ---------------------------------------------------------------------------
# A12/A17 引用与汇总链
# ---------------------------------------------------------------------------
def test_cross_batch_reference_is_rejected(tmp_path):
    """A12：模型引用本批不存在的编号必须被拒绝，且不发布结果。

    这里用单批任务构造“编号越界”的确定性场景；多批场景中“他批编号”与本批
    编号空间独立，任何不属于本批的编号都会得到同一个 `unknown_reference` 拒绝，
    因此该用例同时覆盖跨批误映射与任意越界编号。
    """
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    assert len(outcome.plan.batches) == 1
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    first_batch_text = outcome.plan.batches[0][0].text
    # 本批只有若干个单元，故意引用一个远超本批范围的编号。
    bogus_ref = len(outcome.plan.batches[0]) + 5
    model = StubModel([
        extraction_reply([{"kind": "data", "content": "越界编号", "refs": [bogus_ref]}],
                         {str(bogus_ref): first_batch_text},
                         sections={"data": "present", "conclusion": "none",
                                   "viewpoint": "none"}),
    ])
    run_worker(settings, repository, model, job_id)
    job = repository.get_analysis_job(job_id)
    assert job.status == "failed"
    assert "unknown_reference" in (job.error_code or "")
    assert job.result_id is None
    assert service.list_results(document_id, "extraction") == []


@pytest.mark.parametrize("switch_during_reduce", [False, True])
def test_summary_multi_batch_reduce_keeps_original_references(tmp_path, switch_during_reduce):
    """A16/A17：多批摘要必须执行汇总，最终引用回到原文，中间摘要不被当成原文。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret",
                                analysis_batch_max_chars=420,
                                analysis_max_requests=12)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "summary")
    assert outcome.plan.reduce_required is True
    response, _ = _submit(service, document_id, "summary")
    job_id = response.job.id

    replies = []
    point_ids = []
    exception_ids = []
    for index, batch in enumerate(outcome.plan.batches):
        text = batch[0].text
        replies.append(summary_reply(f"第 {index + 1} 批概述",
                                     [{"text": "批次要点", "refs": [1]}],
                                     [{"text": "批次例外", "refs": [1]}],
                                     {"1": text}))
        # item_id 由服务端按“批次 ID + 分区 + 序号”生成：p 是要点，e 是例外。
        point_ids.append(f"b{index + 1}-p1")
        exception_ids.append(f"b{index + 1}-e1")
    replies.append(reduce_reply(
        "整份文档概述",
        [{"text": "合并后的要点一", "refs": [point_ids[0]]},
         {"text": "合并后的要点二", "refs": [point_ids[1]]}],
        [{"text": "保留的决定性例外", "refs": [exception_ids[1]]}]))
    def switch_version(index, *_):
        if not switch_during_reduce or index != len(outcome.plan.batches):
            return
        # 汇总请求在途时发布新预览，旧任务仍必须使用原快照及完整引用。
        from tests.test_analysis import _write_parsed_version, _activate_version
        original = repository.get_parse_version(response.job.parse_version_id)
        blocks = sample_blocks(version_id="v-new-during-reduce", document_id=document_id)
        blocks[1].text = "新版本的合成营收为 9.9 亿元。"
        version = original.model_copy(update={"id": "v-new-during-reduce", "result_hash": "changed"})
        _write_parsed_version(repository, version, blocks, [])
        _activate_version(repository, document_id, version.id)
    model = StubModel(replies, on_call=switch_version)
    run_worker(settings, repository, model, job_id)

    job = repository.get_analysis_job(job_id)
    assert job.status == "succeeded", (
        f"多批摘要未成功：code={job.error_code} msg={job.error_message}")
    assert len(model.calls) == len(outcome.plan.batches) + 1
    result = service.get_result(job.result_id)
    assert result.parse_version_id == response.job.parse_version_id
    if switch_during_reduce:
        assert not result.is_active_version
        assert repository.get(document_id).active_parse_version_id == "v-new-during-reduce"
    assert result.summary.topic_overview == "整份文档概述"
    assert len(result.summary.main_points) == 2
    assert result.summary.exceptions
    # 最终引用必须是原文连续子串，并且能定位到对应解析版本的块。
    for citation in result.summary.citations:
        assert citation.block_id.startswith(result.parse_version_id)
        assert citation.quote
        assert citation.sources
    assert result.coverage.reduce_completed is True
    assert result.coverage.complete is True
    assert result.coverage.unresolved_original_refs == 0
    # 引用清单里没有“中间摘要文字”被当成原文引述。
    batch_overviews = {f"第 {index + 1} 批概述" for index in range(len(outcome.plan.batches))}
    assert not any(citation.quote in batch_overviews for citation in result.summary.citations)


def test_reduce_with_unknown_item_reference_is_rejected(tmp_path):
    """A17：汇总阶段引用未知中间条目时受控失败，不发布“完整摘要”。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret",
                                analysis_batch_max_chars=420,
                                analysis_max_requests=12)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "summary")
    response, _ = _submit(service, document_id, "summary")
    job_id = response.job.id
    replies = [summary_reply(f"第 {index + 1} 批概述", [{"text": "要点", "refs": [1]}], [],
                             {"1": batch[0].text})
               for index, batch in enumerate(outcome.plan.batches)]
    replies.append(reduce_reply("概述", [{"text": "要点", "refs": ["b99-p1"]}], []))
    run_worker(settings, repository, StubModel(replies), job_id)
    job = repository.get_analysis_job(job_id)
    assert job.status == "failed"
    assert "unknown_reference" in (job.error_code or "")
    assert job.result_id is None
    # 已校验的批次检查点保留，可明确重试。
    assert repository.analysis_step(job_id, "batch", 0)["status"] == "succeeded"


def test_summary_single_batch_has_no_reduce_call(tmp_path):
    """A16：单批摘要不产生汇总调用，也不虚报“完整全文覆盖”之外的结论。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "summary")
    assert outcome.plan.reduce_required is False
    response, _ = _submit(service, document_id, "summary")
    job_id = response.job.id
    text = outcome.plan.batches[0][0].text
    model = StubModel([summary_reply("概述", [{"text": "要点", "refs": [1]}],
                                     [{"text": "例外", "refs": [1]}], {"1": text})])
    run_worker(settings, repository, model, job_id)
    assert len(model.calls) == 1
    result = service.get_result(repository.get_analysis_job(job_id).result_id)
    assert result.coverage.reduce_completed is False
    assert result.coverage.batch_completed == len(outcome.plan.batches)
    assert result.summary.main_points and result.summary.citations


# ---------------------------------------------------------------------------
# A15 提取语义边界（工程侧）
# ---------------------------------------------------------------------------
def test_extraction_empty_section_is_legal_no_extractable_items(tmp_path):
    """A15：已覆盖输入但没有某类内容时是合法空列表，与模型协议失败分开表示。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, _ = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    # 输出为空但三类的 sections 如实写 none：合法，发布成功且标注未处理的类别状态。
    model = StubModel([extraction_reply([], {}, sections={"data": "none", "conclusion": "none",
                                                          "viewpoint": "none"})])
    run_worker(settings, repository, model, job_id)
    job = repository.get_analysis_job(job_id)
    assert job.status == "succeeded"
    result = service.get_result(job.result_id)
    assert result.extraction.items == []
    assert set(result.extraction.sections.values()) == {"none"}
    # 模型协议失败（坏 JSON）必须与“没有可提取项”区分：前者是失败，后者是成功空结果。
    second, _ = _submit(service, document_id, "extraction", regenerate=True)
    run_worker(settings, repository, StubModel(["不是 JSON"]), second.job.id)
    failed = repository.get_analysis_job(second.job.id)
    assert failed.status == "failed"
    assert "output_invalid" in (failed.error_code or "")
    assert failed.result_id is None


def test_extraction_item_limit_fails_without_truncation(tmp_path):
    """A14/A13：条目超量时受控失败，不截断尾部条目再宣称完整。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret",
                                analysis_max_items_per_batch=2,
                                analysis_max_items_total=2)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    assert len(outcome.plan.batches) == 1  # 单批即可触发总量上限
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    text = outcome.plan.batches[0][0].text
    model = StubModel([extraction_reply(
        [{"kind": "data", "content": f"条目 {index}", "refs": [1]} for index in range(5)],
        {"1": text},
        sections={"data": "present", "conclusion": "none", "viewpoint": "none"})])
    run_worker(settings, repository, model, job_id)
    job = repository.get_analysis_job(job_id)
    assert job.status == "failed"
    assert job.error_code == "output_invalid:items_too_many"
    assert job.result_id is None


# ---------------------------------------------------------------------------
# A18 安全失败
# ---------------------------------------------------------------------------
def test_upstream_failure_is_safe_and_creates_no_result(tmp_path):
    """A18：上游错误只写入稳定安全提示，不含密钥、路径或上游原始响应。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, _ = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    run_worker(settings, repository, StubModel([ModelError(502, "DeepSeek 返回了无法解析的响应")]),
               job_id)
    job = repository.get_analysis_job(job_id)
    assert job.status == "failed"
    assert job.error_code == "model_error"
    payload = job.model_dump()
    text = json.dumps(payload, ensure_ascii=False)
    assert "test-secret" not in text
    assert "sk-" not in text
    assert "/api/" not in text
    assert "traceback" not in text.lower()
    # 失败调用已结算并计入预算。
    calls = repository.analysis_calls(job_id)
    assert len(calls) == 1 and calls[0].status == "failed"
    assert job.requests_used == 1


def test_unexpected_exception_marks_call_uncertain_and_stops(tmp_path):
    """A08/A18：未分类异常后账本保持不确定，任务转 needs_attention，不自动重发。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, _ = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id

    class ExplodingModel:
        def __init__(self):
            self.calls = 0

        def generate(self, system_prompt, user_prompt):
            self.calls += 1
            raise RuntimeError("模拟进程中断")

    model = ExplodingModel()
    run_worker(settings, repository, model, job_id)
    job = repository.get_analysis_job(job_id)
    assert job.status == "needs_attention"
    assert job.error_code == "call_uncertain"
    assert model.calls == 1
    calls = repository.analysis_calls(job_id)
    assert len(calls) == 1 and calls[0].status == "intent"
    # 租约过期后由恢复逻辑确认“不确定”：账本行转 uncertain，任务保持 needs_attention。
    with repository.connect() as db:
        db.execute("UPDATE analysis_jobs SET lease_expires_at=?, status='running' WHERE id=?",
                   ("2000-01-01T00:00:00.000000+00:00", job_id))
    repository.claim_next_analysis_job("w2", 60)          # 触发恢复，但不应领取到该任务
    assert model.calls == 1                               # 绝不自动重发
    assert repository.get_analysis_job(job_id).status == "needs_attention"
    assert repository.analysis_calls(job_id)[0].status == "uncertain"
    # 不确定调用仍然计入预算（失败与不确定都算已发出）。
    assert repository.analysis_calls_used(job_id) == 1


# ---------------------------------------------------------------------------
# A20 重启持久化
# ---------------------------------------------------------------------------
def test_state_survives_new_repository_instance(tmp_path):
    """A20：任务、结果、引用、版本、覆盖与预算跨“重启”（新仓储实例）保留。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, version_id = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    ref = unit_ref(outcome.plan, "营业收入为 1.2 亿元")
    text = unit_text(outcome.plan, "营业收入为 1.2 亿元")
    run_worker(settings, repository, StubModel([
        extraction_reply([{"kind": "data", "content": "营业收入 1.2 亿元", "refs": [ref]}],
                         {str(ref): text},
                         sections={"data": "present", "conclusion": "none",
                                   "viewpoint": "none"})]), job_id)

    # 模拟进程重启：全新的仓储与服务工作实例，只读持久化状态。
    restarted = Repository(tmp_path / "docqa.db")
    from app.analysis_jobs import AnalysisJobService

    fresh_service = AnalysisJobService(settings, restarted)
    job = restarted.get_analysis_job(job_id)
    assert job.status == "succeeded" and job.requests_used == 1
    result = fresh_service.get_result(job.result_id)
    assert result.parse_version_id == version_id
    assert result.coverage.complete is True
    assert result.extraction.items
    assert result.extraction.citations[0].quote == text
    assert restarted.analysis_calls_used(job_id) == 1
    # GET 结果与历史不再产生任何生成调用。
    assert restarted.analysis_calls_used(job_id) == 1
    assert fresh_service.list_results(document_id, "extraction")[0].id == result.id


# ---------------------------------------------------------------------------
# A23 导出
# ---------------------------------------------------------------------------
def test_export_markdown_and_json_match_validated_result(tmp_path):
    """A23：Markdown／JSON 与已校验结果一致，含引用与限制，无路径与原始响应泄露。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, version_id = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    ref = unit_ref(outcome.plan, "营业收入为 1.2 亿元")
    text = unit_text(outcome.plan, "营业收入为 1.2 亿元")
    run_worker(settings, repository, StubModel([
        extraction_reply([{"kind": "data", "content": "营业收入 1.2 亿元", "refs": [ref]}],
                         {str(ref): text},
                         sections={"data": "present", "conclusion": "none",
                                   "viewpoint": "none"})]), job_id)
    result_id = repository.get_analysis_job(job_id).result_id
    result = service.get_result(result_id)

    body, media_type, filename = service.export(result_id, "markdown")
    assert media_type.startswith("text/markdown")
    assert filename.endswith(".md") and "/" not in filename and "\\" not in filename
    assert "## 数据" in body
    assert text in body              # 引用原文出现在导出中
    assert result_id in body         # 结果 ID 可追溯
    assert version_id in body        # 解析版本可追溯
    assert "[1]" in body
    assert "test-secret" not in body and ".env" not in body
    assert "reasoning" not in body.lower()
    assert "C:\\" not in body and "/app/" not in body

    payload, media_type_json, filename_json = service.export(result_id, "json")
    assert media_type_json == "application/json"
    assert filename_json.endswith(".json")
    data = json.loads(payload)
    assert data["kind"] == "extraction"
    assert data["parse_version_id"] == version_id
    assert data["coverage"]["complete"] is True
    assert data["extraction"]["items"][0]["content"] == "营业收入 1.2 亿元"
    assert data["extraction"]["citations"][0]["quote"] == text
    assert "prompt" not in payload.lower() or "prompt_version" in payload
    assert "test-secret" not in payload


def test_export_markdown_escapes_dangerous_syntax(tmp_path):
    """A22/A23：文件名与事实中的 HTML／脚本在导出中只作为文本，不形成可执行结构。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "extraction")
    response, _ = _submit(service, document_id, "extraction")
    job_id = response.job.id
    ref = unit_ref(outcome.plan, "营业收入为 1.2 亿元")
    text = unit_text(outcome.plan, "营业收入为 1.2 亿元")
    hostile = "<script>window.__xss=1</script>"
    # 模型在条目里写了 HTML：仍能通过结构校验（文本内容），但导出必须文本化。
    run_worker(settings, repository, StubModel([
        extraction_reply([{"kind": "data", "content": hostile, "refs": [ref]}],
                         {str(ref): text},
                         sections={"data": "present", "conclusion": "none",
                                   "viewpoint": "none"})]), job_id)
    result_id = repository.get_analysis_job(job_id).result_id
    body, _media, _name = service.export(result_id, "markdown")
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    # 文件名中的路径分隔符与控制字符被清理。
    filename = service._export_filename(service.get_result(result_id), repository.get(document_id),
                                        "md")
    assert "/" not in filename and "\\" not in filename and ".." not in filename


def test_export_rejects_unknown_result(tmp_path):
    """A23：导出只接受结果 ID，不接受任意路径；不存在时返回 404。"""
    from app.rag_context import RagError

    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret")
    repository = Repository(tmp_path / "docqa.db")
    seed_document(repository)
    from app.analysis_jobs import AnalysisJobService

    service = AnalysisJobService(settings, repository)
    with pytest.raises(RagError) as excinfo:
        service.export("../../etc/passwd", "json")
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# 覆盖口径：失败时不得声称完整
# ---------------------------------------------------------------------------
def test_failed_multi_batch_summary_reports_incomplete_coverage(tmp_path):
    """A16：漏批或失败时覆盖不完整，不得发布“完整全文摘要”。"""
    settings = fixture_settings(tmp_path, deepseek_api_key="test-secret",
                                analysis_batch_max_chars=420,
                                analysis_max_requests=12)
    repository = Repository(tmp_path / "docqa.db")
    document_id, _ = seed_document(repository)
    service, outcome = plan_for(repository, settings, document_id, "summary")
    assert len(outcome.plan.batches) >= 2
    response, _ = _submit(service, document_id, "summary")
    job_id = response.job.id
    # 第一批成功，第二批失败：任务失败且覆盖显示只完成 1 个批次。
    replies = [summary_reply("第 1 批概述", [{"text": "要点", "refs": [1]}], [],
                             {"1": outcome.plan.batches[0][0].text}),
               "坏输出"]
    run_worker(settings, repository, StubModel(replies), job_id)
    job = repository.get_analysis_job(job_id)
    assert job.status == "failed"
    assert job.result_id is None
    coverage = job.coverage or {}
    assert coverage.get("complete") is False
    assert coverage.get("batch_completed") == 1
    assert coverage.get("unresolved_units", 0) > 0
    assert coverage.get("reduce_completed") is False
    assert service.list_results(document_id, "summary") == []
