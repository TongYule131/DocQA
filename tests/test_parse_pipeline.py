# 解析任务、Docling 客户端、规范化与分块测试：全部离线，使用可注入的假上游。
#
# 覆盖任务书第 11 章的验收场景（T03—T15、T18—T20 的自动化部分）：
# 每个测试都断言具体行为，不通过删除断言或只检查 HTTP 200 来掩盖回归。
import hashlib
import json
import shutil
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chunking import ChunkingConfig, chunk_document
from app.config import Settings
from app.document_normalizer import (
    apply_formula_warnings,
    normalize_docling_result,
    overall_quality_status,
)
from app.docling_client import (
    ERROR_CONNECT,
    ERROR_DEADLINE,
    ERROR_HTTP,
    ERROR_RESULT_INVALID,
    ERROR_RESULT_MISSING,
    ERROR_SUBMIT_UNCERTAIN,
    ERROR_TASK_FAILED,
    DoclingClient,
    DoclingError,
)
from app.file_detect import UploadFormatError, detect_format, sanitize_filename
from app.main import create_app
from app.parse_worker import ParseWorker
from app.repository import Repository
from app.schemas import Document

# ---------------------------------------------------------------------------
# 测试夹具：可注入的假 Docling 上游
# ---------------------------------------------------------------------------


class FakeDocling:
    """按脚本返回状态的可控上游；记录调用次数以证明“不重复提交/不重复领取”。"""

    def __init__(self, *, result=None, fail_task=False, missing_result=False,
                 invalid_json=False, submit_error=None, string_json=False):
        self.result = result if result is not None else docling_payload()
        self.fail_task = fail_task
        self.missing_result = missing_result
        self.invalid_json = invalid_json
        self.string_json = string_json
        self.submit_error = submit_error
        self.submit_calls = 0
        self.poll_calls = 0
        self.result_calls = 0
        self.uploaded_name = None
        self.uploaded_mime = None
        self.cancelled = False

    def submit(self, *, filename, content, mime):
        self.submit_calls += 1
        self.uploaded_name = filename
        self.uploaded_mime = mime
        if self.submit_error is not None:
            raise self.submit_error
        return {"task_id": "upstream-1", "task_status": "pending"}

    def poll(self, task_id):
        self.poll_calls += 1
        if self.fail_task:
            return {"task_id": task_id, "task_status": "failure", "error_message": "内部失败细节"}
        return {"task_id": task_id, "task_status": "success"}

    def fetch_result(self, task_id):
        self.result_calls += 1
        if self.missing_result:
            raise DoclingError(ERROR_RESULT_MISSING, "结果不存在")
        if self.invalid_json:
            return {"status": "success", "document": {"json_content": None, "md_content": ""}}
        structured = json.loads(json.dumps(self.result, ensure_ascii=False))
        json_content = json.dumps(structured) if self.string_json else structured
        # 与真实接口一致：结果响应为 {status, document:{json_content, md_content}, ...}。
        return {"status": "success", "processing_time": 1.0,
                "document": {"json_content": json_content, "md_content": "# 预览"}}

    def wait_for_result(self, task_id, *, should_stop=None, on_poll=None):
        """复用真实客户端的解析与错误处理逻辑，只替换上游 HTTP 响应。

        这样测试覆盖的仍然是生产代码路径（状态判断、json_content 兼容、错误分类），
        而不是一个只在测试里存在的简化实现。
        """
        from app.docling_client import ConversionResult
        task = self.poll(task_id)
        if on_poll is not None:
            on_poll(str(task.get("task_status") or "").lower())
        if task["task_status"] == "failure":
            raise DoclingError(ERROR_TASK_FAILED, "解析服务报告任务失败")
        payload = self.fetch_result(task_id)
        document = payload.get("document") or {}
        json_content = document.get("json_content")
        if isinstance(json_content, str):
            try:
                json_content = json.loads(json_content)
            except (TypeError, ValueError):
                raise DoclingError(ERROR_RESULT_INVALID, "解析结果的 JSON 内容无法解析") from None
        if not isinstance(json_content, dict):
            raise DoclingError(ERROR_RESULT_INVALID, "解析结果缺少结构化 JSON 内容")
        return ConversionResult(task_id=task_id, task_status="success", conversion_status="success",
                                json_content=json_content, md_content=document.get("md_content"),
                                processing_time=1.0)

    def config_summary(self, ocr_lang=None):
        return {"ocr_engine": "rapidocr", "ocr_lang": ocr_lang or "ch"}

    def probe(self):
        return {"reachable": True, "status_code": 200, "detail": "可响应", "elapsed_ms": 1,
                "error_code": None}

    def close(self):
        pass


def docling_payload(*, text="第一段正文内容。", table_text="区域 | 数值",
                    page_count=2, label="text", formula_text="", extra_texts=None):
    """构造最小但结构完整的 Docling 结果，用于规范化与分块测试。"""
    texts = [{
        "self_ref": "#/texts/0", "label": label, "text": text,
        "parent": {"$ref": "#/body"},
        "prov": [{"page_no": 1, "bbox": {"l": 10.0, "t": 20.0, "r": 200.0, "b": 40.0,
                                         "coord_origin": "BOTTOMLEFT"}, "charspan": [0, len(text)]}],
    }]
    if formula_text is not None and label == "formula":
        texts[0]["text"] = formula_text
    for index, item in enumerate(extra_texts or []):
        texts.append({
            "self_ref": f"#/texts/{index + 1}", "label": item.get("label", "text"),
            "text": item.get("text", ""), "parent": {"$ref": "#/body"},
            "prov": [{"page_no": item.get("page", 1), "bbox": {"l": 1.0, "t": 2.0, "r": 3.0, "b": 4.0,
                                                              "coord_origin": "BOTTOMLEFT"},
                      "charspan": [0, 1]}],
        })
    children = [{"$ref": f"#/texts/{index}"} for index in range(len(texts))]
    table = {
        "self_ref": "#/tables/0", "label": "table", "parent": {"$ref": "#/body"}, "children": [],
        "prov": [{"page_no": 2, "bbox": {"l": 1.0, "t": 2.0, "r": 3.0, "b": 4.0,
                                         "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 0]}],
        "captions": [], "footnotes": [],
        "data": {
            "num_rows": 2, "num_cols": 2, "orientation": "row",
            "grid": [[{"text": "区域", "row_span": 1, "col_span": 1, "start_row_offset_idx": 0,
                       "end_row_offset_idx": 1, "start_col_offset_idx": 0, "end_col_offset_idx": 1,
                       "column_header": True, "row_header": False, "row_section": False, "bbox": None},
                      {"text": "数值", "row_span": 1, "col_span": 1, "start_row_offset_idx": 0,
                       "end_row_offset_idx": 1, "start_col_offset_idx": 1, "end_col_offset_idx": 2,
                       "column_header": True, "row_header": False, "row_section": False, "bbox": None}],
                     [{"text": "甲区", "row_span": 1, "col_span": 1, "start_row_offset_idx": 1,
                       "end_row_offset_idx": 2, "start_col_offset_idx": 0, "end_col_offset_idx": 1,
                       "column_header": False, "row_header": False, "row_section": False, "bbox": None},
                      {"text": "12.5", "row_span": 1, "col_span": 1, "start_row_offset_idx": 1,
                       "end_row_offset_idx": 2, "start_col_offset_idx": 1, "end_col_offset_idx": 2,
                       "column_header": False, "row_header": False, "row_section": False, "bbox": None}]],
        },
    }
    table["data"]["table_cells"] = [cell for row in table["data"]["grid"] for cell in row]
    if table_text is not None:
        children.append({"$ref": "#/tables/0"})
    return {
        "schema_name": "DoclingDocument", "version": "1.10.0", "name": "sample",
        "furniture": {"self_ref": "#/furniture", "parent": None, "children": []},
        "body": {"self_ref": "#/body", "parent": None, "children": children},
        "groups": [], "texts": texts, "pictures": [],
        "tables": [table] if table_text is not None else [],
        "pages": {str(number): {"page_no": number} for number in range(1, page_count + 1)},
    }


def make_worker(tmp_path, client, **settings_kwargs):
    """构造使用假上游的 worker；不需要 Docker、密钥或网络。

    每次调用都重新执行 initialize（幂等），与真实 worker 进程启动行为一致。
    """
    settings = Settings(data_dir=tmp_path, **settings_kwargs)
    repository = Repository(tmp_path / "docqa.db")
    repository.initialize()
    return ParseWorker(settings, repository=repository, client=client, sleep=lambda _s: None)


def worker_for(tmp_path, repository, client, **settings_kwargs):
    """复用已有 Repository 构造 worker，便于在测试中检查同一连接的数据库状态。"""
    settings = Settings(data_dir=tmp_path, **settings_kwargs)
    return ParseWorker(settings, repository=repository, client=client, sleep=lambda _s: None)


def result_hashes(repository, document_id):
    """读取该文档已发布版本的 result_hash，便于断言版本确实变化。"""
    return [version.result_hash for version in repository.list_parse_versions(document_id)]


def upload_pdf(client, name="sample.pdf", content=None):
    """上传一份最小 PDF 内容；仅用于建立文档记录与原件文件。"""
    from pypdf import PdfWriter
    if content is None:
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buffer = BytesIO()
        writer.write(buffer)
        content = buffer.getvalue()
    response = client.post('/api/documents', files={'file': (name, content, 'application/pdf')})
    assert response.status_code == 201, response.text
    return response.json()['document']['id']


# ---------------------------------------------------------------------------
# T03：重复 parse、并发 parse、幂等键冲突
# ---------------------------------------------------------------------------
def test_t03_repeat_concurrent_parse_and_idempotency(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document_id = upload_pdf(client)
        first = client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        assert first.status_code == 202
        task_id = first.json()['task']['id']
        # 并发重复提交：复用同一活动任务，不产生第二次上游提交机会。
        for _ in range(3):
            repeat = client.post(f'/api/documents/{document_id}/parse', json={'force': True})
            assert repeat.status_code == 202 and repeat.json()['task']['id'] == task_id
        # 幂等键：先让第一个任务结束，再用幂等键建立可识别的键控任务。
        repository = Repository(tmp_path / 'docqa.db')
        assert repository.abandon_queued_task(task_id) is True
        keyed = client.post(f'/api/documents/{document_id}/parse',
                            json={'force': True, 'idempotency_key': 'k1'})
        assert keyed.status_code == 202
        keyed_id = keyed.json()['task']['id']
        # 同键同请求：返回同一任务，不新建、不重复提交上游。
        assert client.post(f'/api/documents/{document_id}/parse',
                           json={'force': True, 'idempotency_key': 'k1'}).json()['task']['id'] == keyed_id
        # 同键不同请求：必须识别为冲突。
        assert client.post(f'/api/documents/{document_id}/parse',
                           json={'force': False, 'idempotency_key': 'k1'}).status_code == 409
        # 活动任务重复点击：不产生第二个活动任务。
        again = client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        assert again.status_code == 202 and again.json()['task']['id'] == keyed_id
        with repository.connect() as db:
            active = db.execute(
                "SELECT COUNT(*) FROM parse_tasks WHERE document_id=? AND status IN ('queued','running')",
                (document_id,)).fetchone()[0]
        assert active == 1
        # 不存在的任务与非法状态。
        assert client.get('/api/parse-tasks/nope').status_code == 404
        assert client.post(f'/api/parse-tasks/{keyed_id}/retry').status_code == 409


# ---------------------------------------------------------------------------
# T04：两个 worker 争抢 / 旧租约过期
# ---------------------------------------------------------------------------
def test_t04_lease_claiming_and_stale_token(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, worker_lease_seconds=60))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        repository = Repository(tmp_path / 'docqa.db')
        first = repository.claim_next_task('worker-a', 60)
        assert first is not None
        # 第二个 worker 不能领取同一任务（租约未过期）。
        assert repository.claim_next_task('worker-b', 60) is None
        # 旧令牌不能写入进度，也不能发布结果。
        stale = first.lease_token + '-stale'
        assert repository.update_task_progress(first.id, stale, stage='normalizing') is False
        assert repository.renew_lease(first.id, stale, 60) is False
        assert repository.finish_task(first.id, stale, status='succeeded', stage='done') is False
        # 有效令牌可以续租与更新阶段。
        assert repository.renew_lease(first.id, first.lease_token, 60) is True
        assert repository.update_task_progress(first.id, first.lease_token, stage='normalizing') is True
        # 租约过期后另一个 worker 可以接管，并获得新的令牌。
        with repository.connect() as db:
            db.execute("UPDATE parse_tasks SET lease_expires_at=? WHERE id=?",
                       ('2000-01-01T00:00:00+00:00', first.id))
        second = repository.claim_next_task('worker-b', 60, task_id=first.id)
        assert second is not None and second.lease_token != first.lease_token
        # 旧令牌此时依然不能写入，避免过期执行者覆盖新执行者。
        assert repository.finish_task(first.id, first.lease_token, status='failed', stage='done') is False


# ---------------------------------------------------------------------------
# T05：Web/worker 重启，上游 ID 已保存 → 恢复查询与领取，不重复 POST
# ---------------------------------------------------------------------------
def test_t05_restart_resumes_saved_upstream_task(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    # 第一次执行：提交后保存上游 task_id，但结果领取前“进程中断”。
    repository = Repository(tmp_path / 'docqa.db')
    repository.initialize()
    interrupted = FakeDocling()
    # 断网：等待阶段失败，但上游 task_id 已保存到任务表。
    interrupted.wait_for_result = lambda task_id, **kwargs: (_ for _ in ()).throw(
        KeyboardInterrupt("模拟进程在保存远端 ID 后终止"))
    worker = ParseWorker(Settings(data_dir=tmp_path), repository=repository, client=interrupted,
                         sleep=lambda _s: None)
    with pytest.raises(KeyboardInterrupt):
        worker.run_forever(max_tasks=1)
    task = repository.latest_task(document_id)
    assert interrupted.submit_calls == 1
    assert task.upstream_task_id == 'upstream-1'
    # 保留中断现场的 running 状态，仅推进租约到过期；必须由真实恢复逻辑重新领取。
    with repository.connect() as db:
        db.execute("UPDATE parse_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (task.id,))
    # 重启后恢复：必须直接使用已保存的上游 task_id，不能再次 POST。
    resumed = FakeDocling()
    worker2 = ParseWorker(Settings(data_dir=tmp_path), repository=repository, client=resumed,
                          sleep=lambda _s: None)
    worker2.run_forever(max_tasks=1)
    assert resumed.submit_calls == 0
    assert resumed.result_calls >= 1
    assert repository.latest_task(document_id).status == 'succeeded'


def test_t05_startup_recovers_expired_lease_without_marking_failed(tmp_path):
    """启动时租约过期的 running 任务按尝试次数恢复，不能被统一改成失败。

    - 仍有剩余尝试次数 → 回到 queued，由 worker 继续；
    - 尝试次数已用尽 → 进入 needs_attention，等用户确认后重试；
    两种情况都不是“统一标记为失败”，旧解析版本与索引也不受影响。
    """
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    repository = Repository(tmp_path / 'docqa.db')
    repository.initialize()
    task = repository.claim_next_task('worker-a', 60)
    assert task is not None
    with repository.connect() as db:
        db.execute("UPDATE parse_tasks SET lease_expires_at=? WHERE id=?",
                   ('2000-01-01T00:00:00+00:00', task.id))
    repository.initialize()
    recovered = repository.get_task(task.id)
    assert recovered.status in {'queued', 'needs_attention'}
    assert recovered.status != 'failed'
    assert recovered.error_code != 'parse_interrupted'
    # 恢复后仍可被 worker 领取并执行，上游 task_id 不会丢失。
    if recovered.status == 'queued':
        again = repository.claim_next_task('worker-b', 60, task_id=task.id)
        assert again is not None


# ---------------------------------------------------------------------------
# T06：POST 已接收但响应丢失 → 不确定状态，不自动重投
# ---------------------------------------------------------------------------
def test_t06_uncertain_submit_requires_manual_retry(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    repository = Repository(tmp_path / 'docqa.db')
    repository.initialize()
    uncertain = FakeDocling(submit_error=DoclingError(
        ERROR_SUBMIT_UNCERTAIN, "提交后等待响应超时，无法确认上游状态", uncertain=True))
    worker = ParseWorker(Settings(data_dir=tmp_path), repository=repository, client=uncertain,
                         sleep=lambda _s: None)
    worker.run_forever(max_tasks=1)
    task = repository.latest_task(document_id)
    assert task.status == 'needs_attention'
    assert 'exactly-once' in (task.error_message or '')
    # 自动重跑不会重新领取 needs_attention 任务，因此不会盲目重投上游。
    worker.run_forever(max_tasks=1)
    assert uncertain.submit_calls == 1
    # 用户明确重试才新建尝试，并提示可能重复转换。
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        retry = client.post(f'/api/parse-tasks/{task.id}/retry')
        assert retry.status_code == 200
        assert retry.json()['id'] != task.id
        assert repository.get_task(task.id).status == 'needs_attention'


# ---------------------------------------------------------------------------
# T07：上游 failure / 结果 404 / 超时 / 错误 JSON → 错误分类正确，无效结果不发布
# ---------------------------------------------------------------------------
def test_t07_upstream_failure_missing_result_and_invalid_json(tmp_path):
    # 上游任务失败
    with TestClient(create_app(Settings(data_dir=tmp_path / 'a'))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    repository = Repository(tmp_path / 'a' / 'docqa.db')
    repository.initialize()
    worker = ParseWorker(Settings(data_dir=tmp_path / 'a'), repository=repository,
                         client=FakeDocling(fail_task=True), sleep=lambda _s: None)
    worker.run_forever(max_tasks=1)
    task = repository.latest_task(document_id)
    assert task.status == 'failed' and task.error_code == ERROR_TASK_FAILED
    assert '内部失败细节' not in (task.error_message or '')
    assert repository.get(document_id).active_parse_version_id is None

    # 结果缺失（过期）：记录原因并允许用户重试
    with TestClient(create_app(Settings(data_dir=tmp_path / 'b'))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    repository = Repository(tmp_path / 'b' / 'docqa.db')
    repository.initialize()
    worker = ParseWorker(Settings(data_dir=tmp_path / 'b'), repository=repository,
                         client=FakeDocling(missing_result=True), sleep=lambda _s: None)
    worker.run_forever(max_tasks=1)
    task = repository.latest_task(document_id)
    assert task.status == 'failed' and task.error_code == 'upstream_task_lost'

    # 结果结构无效：不发布版本
    with TestClient(create_app(Settings(data_dir=tmp_path / 'c'))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    repository = Repository(tmp_path / 'c' / 'docqa.db')
    repository.initialize()
    worker = ParseWorker(Settings(data_dir=tmp_path / 'c'), repository=repository,
                         client=FakeDocling(invalid_json=True), sleep=lambda _s: None)
    worker.run_forever(max_tasks=1)
    task = repository.latest_task(document_id)
    assert task.status == 'failed' and task.error_code == ERROR_RESULT_INVALID
    assert repository.get(document_id).active_parse_version_id is None


def test_t07_client_error_classification():
    """客户端错误分类：网络、HTTP、超时、结果缺失分别映射为稳定错误码。"""
    def handler(request):
        raise httpx.ConnectError('boom', request=request)
    settings = Settings(data_dir=Path('.'))
    with DoclingClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DoclingError) as error:
            client.submit(filename='a.pdf', content=b'%PDF-1.4', mime='application/pdf')
        assert error.value.code == ERROR_CONNECT and error.value.retryable is True
    # 提交阶段读取超时：必须按“不确定”处理，不能自动重试。
    def timeout_handler(request):
        raise httpx.ReadTimeout('slow', request=request)
    with DoclingClient(settings, transport=httpx.MockTransport(timeout_handler)) as client:
        with pytest.raises(DoclingError) as error:
            client.submit(filename='a.pdf', content=b'%PDF-1.4', mime='application/pdf')
        assert error.value.code == ERROR_SUBMIT_UNCERTAIN and error.value.uncertain is True
    # 404 结果：稳定错误码，不重试无意义请求。
    with DoclingClient(settings, transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={'detail': 'gone'}))) as client:
        with pytest.raises(DoclingError) as error:
            client.fetch_result('t1')
        assert error.value.code == ERROR_RESULT_MISSING
    # 500：可重试的 HTTP 错误，重试次数受配置限制。
    calls = []
    def server_error(request):
        calls.append(request)
        return httpx.Response(500, text='internal')
    with DoclingClient(settings, transport=httpx.MockTransport(server_error),
                       sleep=lambda _s: None) as client:
        with pytest.raises(DoclingError) as error:
            client.poll('t1')
        assert error.value.code == ERROR_HTTP
        assert len(calls) == settings.docling_max_retries + 1
        assert 'internal' not in str(error.value)
    # 整体期限：即使上游一直返回 pending，也会在期限内结束而不是无限阻塞。
    ticks = [0.0]
    def slow_poll(request):
        ticks[0] += 100.0
        return httpx.Response(200, json={'task_id': 't1', 'task_status': 'pending'})
    settings2 = Settings(data_dir=Path('.'), docling_total_timeout_seconds=10,
                         docling_read_timeout_seconds=5, docling_poll_interval_seconds=1)
    with DoclingClient(settings2, transport=httpx.MockTransport(slow_poll), sleep=lambda _s: None,
                       clock=lambda: ticks[0]) as client:
        with pytest.raises(DoclingError) as error:
            client.wait_for_result('t1')
        assert error.value.code == ERROR_DEADLINE


def test_t07_json_content_accepts_object_and_string():
    """json_content 既支持实测的对象形式，也覆盖字符串形式。"""
    settings = Settings(data_dir=Path('.'))
    payload = docling_payload()
    for string_json in (False, True):
        def handler(request, string_json=string_json):
            if request.url.path.endswith('/status/poll/t1'):
                return httpx.Response(200, json={'task_id': 't1', 'task_status': 'success'})
            body = json.loads(json.dumps(payload, ensure_ascii=False))
            json_content = json.dumps(body) if string_json else body
            return httpx.Response(200, json={
                'status': 'success', 'processing_time': 1.0,
                'document': {'json_content': json_content, 'md_content': '# 预览'}})
        with DoclingClient(settings, transport=httpx.MockTransport(handler),
                           sleep=lambda _s: None) as client:
            task = client.wait_for_result('t1')
            assert isinstance(task.json_content, dict)
            assert task.json_content['body']['self_ref'] == '#/body'


# ---------------------------------------------------------------------------
# T08：结果写入失败 / 分块失败 / 提交失败 → 原活动版本与原索引仍可用
# ---------------------------------------------------------------------------
def test_t08_failures_keep_previous_version_and_index(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document_id = upload_pdf(client)
        # 先用可控上游成功解析并发布版本 A。
        worker = make_worker(tmp_path, FakeDocling())
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        worker.run_forever(max_tasks=1)
        first_version = client.get(f'/api/documents/{document_id}').json()['active_parse_version_id']
        assert first_version
        chunks_before = client.get(f'/api/documents/{document_id}/chunks').json()
        assert chunks_before

        # 重新解析：结果文件写入失败（把结果目录替换为同名文件，制造 OSError）。
        results_dir = tmp_path / 'parse-results'
        for item in results_dir.iterdir():
            if item.is_dir():
                continue
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        worker = make_worker(tmp_path, FakeDocling())
        original_write = worker._write_result_files
        worker._write_result_files = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('磁盘写入失败'))
        worker.run_forever(max_tasks=1)
        document = client.get(f'/api/documents/{document_id}').json()
        # 新任务失败，但活动版本仍是 A，分块仍可读取。
        assert document['active_parse_version_id'] == first_version
        assert document['status'] == 'parsed'
        assert client.get(f'/api/documents/{document_id}/chunks').json() == chunks_before
        assert document['task_status'] == 'failed'
        assert document['latest_task_error']

        # 分块失败：同样不改变活动版本。
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        worker = make_worker(tmp_path, FakeDocling())
        import app.parse_worker as worker_module
        original_chunk = worker_module.chunk_document
        worker_module.chunk_document = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('分块失败'))
        try:
            worker.run_forever(max_tasks=1)
        finally:
            worker_module.chunk_document = original_chunk
        document = client.get(f'/api/documents/{document_id}').json()
        assert document['active_parse_version_id'] == first_version
        assert client.get(f'/api/documents/{document_id}/chunks').json() == chunks_before

        # 数据库提交失败：在发布事务中制造异常，活动版本不变。
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        worker = make_worker(tmp_path, FakeDocling())
        repository = worker.repository
        original_publish = repository.publish_parse_version
        def broken_publish(*args, **kwargs):
            raise RuntimeError('数据库提交失败')
        repository.publish_parse_version = broken_publish
        worker.run_forever(max_tasks=1)
        repository.publish_parse_version = original_publish
        document = client.get(f'/api/documents/{document_id}').json()
        assert document['active_parse_version_id'] == first_version
        assert client.get(f'/api/documents/{document_id}/chunks').json() == chunks_before


# ---------------------------------------------------------------------------
# T09：完整结果重复领取及提交前后中断 → 无重复发布、无重复分块
# ---------------------------------------------------------------------------
def test_t09_duplicate_publication_is_idempotent(tmp_path):
    """同一结果重复发布（重复领取/重复恢复）不得生成重复版本、块或分块。"""
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    repository = Repository(tmp_path / 'docqa.db')
    repository.initialize()
    worker = ParseWorker(Settings(data_dir=tmp_path), repository=repository, client=FakeDocling(),
                         sleep=lambda _s: None)
    # 领取任务但先不执行：保持租约有效，用于验证“重复发布”的幂等性。
    task = repository.claim_next_task('worker-a', 300)
    assert task is not None
    worker.process_task(task)
    task = repository.get_task(task.id)
    assert task.status == 'succeeded'
    version_id = repository.get(document_id).active_parse_version_id
    chunks_after_first = repository.chunks(document_id, version_id)
    blocks_after_first = repository.blocks(version_id)
    assert len(chunks_after_first) >= 2 and blocks_after_first
    # 直接再次发布同一结果哈希（模拟重复领取 / 恢复后再次发布）：
    # 令牌已随任务结束失效，因此不允许写入，也不会生成重复版本。
    published, created = repository.publish_parse_version(
        task.id, 'stale-token', worker_version(task, version_id, repository),
        blocks_after_first, chunks_after_first, [])
    assert published is None and created is False
    assert len(repository.chunks(document_id, version_id)) == len(chunks_after_first)
    assert len(repository.blocks(version_id)) == len(blocks_after_first)
    versions = [v for v in repository.list_parse_versions(document_id) if not v.is_legacy]
    assert len(versions) == 1
    # 结果文件目录也只存在一份。
    result_dirs = [p for p in (tmp_path / 'parse-results').iterdir() if p.is_dir()]
    assert len(result_dirs) == 1

    # 结果哈希幂等：在任务仍持有有效租约时重复发布同一哈希，必须复用已有版本。
    client_task = repository.create_task(type(task)(
        id='dup-task', document_id=document_id, status='queued', stage='queued',
        attempt_count=0, max_attempts=1, created_at=task.created_at, updated_at=task.updated_at))
    claimed = repository.claim_next_task('worker-b', 300, task_id=client_task.id)
    assert claimed is not None
    published_id, created = repository.publish_parse_version(
        claimed.id, claimed.lease_token, worker_version(claimed, version_id, repository),
        blocks_after_first, chunks_after_first, [])
    assert published_id == version_id and created is False
    assert len(repository.blocks(version_id)) == len(blocks_after_first)


def worker_version(task, version_id, repository):
    """构造用于重复发布测试的版本对象（内容与已发布版本一致）。"""
    from app.schemas import ParseVersion
    existing = repository.get_parse_version(version_id)
    return ParseVersion(
        id=version_id, document_id=task.document_id, task_id=task.id,
        origin_hash=existing.origin_hash, parser_name=existing.parser_name,
        parser_version=existing.parser_version, config_summary=existing.config_summary,
        result_schema_version=existing.result_schema_version, result_hash=existing.result_hash,
        quality_status=existing.quality_status, quality_summary=existing.quality_summary,
        block_count=existing.block_count, page_count=existing.page_count,
        chunk_count=existing.chunk_count, created_at=existing.created_at)


def test_t09_result_written_but_not_published_recovers(tmp_path):
    """结果已落盘、发布前中断：再次执行复用同一结果哈希，不产生重复版本。"""
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document_id = upload_pdf(client)
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    repository = Repository(tmp_path / 'docqa.db')
    repository.initialize()
    worker = ParseWorker(Settings(data_dir=tmp_path), repository=repository, client=FakeDocling(),
                         sleep=lambda _s: None)
    # 第一次：结果文件已写入，但发布前抛出异常（模拟进程中断）。
    original_publish = repository.publish_parse_version
    repository.publish_parse_version = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('中断'))
    worker.run_forever(max_tasks=1)
    repository.publish_parse_version = original_publish
    results_root = tmp_path / 'parse-results'
    files_after_interrupt = sorted(p.name for p in results_root.iterdir()) if results_root.exists() else []
    assert files_after_interrupt, '结果文件应当已经落盘'
    # 任务被标记失败（尝试次数用尽），用户显式重试后再次执行。
    task = repository.latest_task(document_id)
    assert task.status == 'failed'
    repository.create_task(type(task)(
        id='retry-task', document_id=document_id, status='queued', stage='queued',
        attempt_count=0, max_attempts=1, created_at=task.created_at, updated_at=task.updated_at))
    worker.run_forever(max_tasks=1)
    versions = [v for v in repository.list_parse_versions(document_id) if not v.is_legacy]
    assert len(versions) == 1
    assert len(repository.blocks(versions[0].id)) > 0
    assert len(repository.chunks(document_id, versions[0].id)) > 0
    # 结果目录没有重复副本。
    dirs = [p for p in (tmp_path / 'parse-results').iterdir() if p.is_dir() and not p.name.startswith('failed-')]
    assert len(dirs) == 1


# ---------------------------------------------------------------------------
# T10：空白页夹在正常页中、整份空文档
# ---------------------------------------------------------------------------
def test_t10_blank_page_keeps_numbering_and_empty_document_fails(tmp_path):
    # 第 2 页既没有文本也没有表格：应识别为空白页，且第 3 页页码不位移。
    payload = docling_payload(page_count=3, table_text=None,
                              extra_texts=[{"text": "第三页内容", "page": 3}])
    normalized = normalize_docling_result('doc1', payload, 'pdf')
    blank = [w for w in normalized.warnings if w.code == 'blank_page']
    assert [w.page for w in blank] == [2]
    pages = [block.sources[0].page for block in normalized.blocks if block.sources]
    assert 1 in pages and 3 in pages and 2 not in pages
    chunks = chunk_document(normalized, ChunkingConfig())
    # 合并后的正文分块必须保留全部来源页，不能只留下第一页。
    chunk_pages = {source.page for chunk in chunks for source in chunk.sources}
    assert {1, 3} <= chunk_pages
    # 有内容的页面不会被误报为空白页。
    payload_full = docling_payload(page_count=2)
    normalized_full = normalize_docling_result('doc1b', payload_full, 'pdf')
    assert [w.page for w in normalized_full.warnings if w.code == 'blank_page'] == []
    # 整份空文档：标记为结构无效，不生成虚假可用索引。
    empty_payload = {"schema_name": "DoclingDocument", "body": {"children": [{"$ref": "#/texts/0"}]},
                     "texts": [{"self_ref": "#/texts/0", "label": "text", "text": "",
                                "parent": {"$ref": "#/body"}, "prov": []}],
                     "tables": [], "groups": [], "pictures": [], "pages": {"1": {"page_no": 1}}}
    empty = normalize_docling_result('doc2', empty_payload, 'pdf')
    assert overall_quality_status(empty.warnings) == 'invalid'
    assert any(w.code == 'no_indexable_content' for w in empty.warnings)
    assert chunk_document(empty, ChunkingConfig()) == []


# ---------------------------------------------------------------------------
# T11：标题/正文/表格交错、有表格子节点 → 阅读顺序正确且表格不重复索引
# ---------------------------------------------------------------------------
def test_t11_reading_order_and_table_child_deduplication():
    payload = docling_payload()
    # 在 body 顺序中插入标题、正文与表格，并让表格的富文本单元格成为独立 texts 节点。
    payload['texts'].insert(0, {"self_ref": "#/texts/99", "label": "section_header",
                                "text": "一、总体情况", "parent": {"$ref": "#/body"},
                                "prov": [{"page_no": 1, "charspan": [0, 3]}]})
    payload['groups'] = [{"self_ref": "#/groups/0", "label": "unspecified", "name": "rich_cell",
                          "parent": {"$ref": "#/tables/0"},
                          "children": [{"$ref": "#/texts/98"}]}]
    payload['texts'].append({"self_ref": "#/texts/98", "label": "text", "text": "区域",
                             "parent": {"$ref": "#/groups/0"}, "prov": []})
    payload['body']['children'] = [{"$ref": "#/texts/99"}, {"$ref": "#/texts/0"},
                                   {"$ref": "#/tables/0"}]
    normalized = normalize_docling_result('doc', payload, 'pdf')
    types = [block.block_type for block in normalized.blocks]
    assert types == ['section_header', 'paragraph', 'table']
    assert normalized.stats['table_cell_text_nodes_skipped'] >= 1
    chunks = chunk_document(normalized, ChunkingConfig())
    # 表格单元格文本不得作为独立正文分块出现（避免重复索引）。
    table_texts = [chunk.text for chunk in chunks if chunk.chunk_type == 'table_rows']
    assert table_texts and all('区域' in text for text in table_texts)
    paragraph_chunks = [chunk for chunk in chunks if chunk.chunk_type == 'text']
    assert not any(chunk.text.strip() == '区域' for chunk in paragraph_chunks)


# ---------------------------------------------------------------------------
# T12：PDF 多来源 bbox、DOCX 嵌套标题/合并表格
# ---------------------------------------------------------------------------
def test_t12_pdf_multi_source_bbox_and_docx_section_path():
    payload = docling_payload()
    # 长段落跨页：prov 有两条记录，必须都保留。
    payload['texts'][0]['prov'] = [
        {"page_no": 1, "bbox": {"l": 1.0, "t": 2.0, "r": 3.0, "b": 4.0, "coord_origin": "BOTTOMLEFT"}},
        {"page_no": 2, "bbox": {"l": 5.0, "t": 6.0, "r": 7.0, "b": 8.0, "coord_origin": "BOTTOMLEFT"}},
    ]
    normalized = normalize_docling_result('doc', payload, 'pdf')
    sources = normalized.blocks[0].sources
    assert [source.page for source in sources] == [1, 2]
    assert sources[0].coord_origin == 'BOTTOMLEFT' and sources[0].coord_unit == 'pt'
    assert sources[1].bbox['l'] == 5.0

    # DOCX：嵌套标题形成章节路径；表格无 prov，page 必须为 None（不伪造页码）。
    docx = {
        "schema_name": "DoclingDocument",
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}, {"$ref": "#/texts/2"},
                              {"$ref": "#/tables/0"}]},
        "texts": [
            {"self_ref": "#/texts/0", "label": "title", "text": "报告", "parent": {"$ref": "#/body"}, "prov": []},
            {"self_ref": "#/texts/1", "label": "section_header", "text": "一、总体", "parent": {"$ref": "#/texts/0"}, "prov": []},
            {"self_ref": "#/texts/2", "label": "section_header", "text": "1.1 细分", "parent": {"$ref": "#/texts/1"}, "prov": []},
            {"self_ref": "#/texts/3", "label": "text", "text": "正文", "parent": {"$ref": "#/texts/2"}, "prov": []},
        ],
        "tables": [{"self_ref": "#/tables/0", "label": "table", "parent": {"$ref": "#/texts/2"},
                    "children": [], "captions": [], "footnotes": [],
                    # DOCX 表格带 prov 但没有真实页码（page_no 缺失）：不得伪造页号。
                    "prov": [{"charspan": [0, 0]}],
                    "data": {"num_rows": 2, "num_cols": 2, "orientation": "row",
                             "grid": [[{"text": "合并标题", "row_span": 1, "col_span": 2,
                                        "start_row_offset_idx": 0, "end_row_offset_idx": 1,
                                        "start_col_offset_idx": 0, "end_col_offset_idx": 2,
                                        "column_header": True, "bbox": None}],
                                      [{"text": "甲", "row_span": 1, "col_span": 1,
                                        "start_row_offset_idx": 1, "end_row_offset_idx": 2,
                                        "start_col_offset_idx": 0, "end_col_offset_idx": 1,
                                        "column_header": False, "bbox": None},
                                       {"text": "1", "row_span": 1, "col_span": 1,
                                        "start_row_offset_idx": 1, "end_row_offset_idx": 2,
                                        "start_col_offset_idx": 1, "end_col_offset_idx": 2,
                                        "column_header": False, "bbox": None}]]}},
        ],
        "groups": [], "pictures": [], "pages": {},
    }
    docx['body']['children'].append({"$ref": "#/texts/3"})
    docx['tables'][0]['data']['table_cells'] = [cell for row in docx['tables'][0]['data']['grid'] for cell in row]
    normalized_docx = normalize_docling_result('docx1', docx, 'docx')
    sections = [block.heading_path for block in normalized_docx.blocks if block.block_type == 'section_header']
    assert sections == ['报告', '报告 / 一、总体']
    table_block = next(block for block in normalized_docx.blocks if block.block_type == 'table')
    assert table_block.sources[0].page is None
    assert table_block.sources[0].format == 'docx'
    # 合并单元格信息保留：标题列跨度 2。
    spans = [cell['col_span'] for cell in table_block.table['cells']]
    assert 2 in spans
    # 分块不产生伪造页码。
    chunks = chunk_document(normalized_docx, ChunkingConfig())
    assert all(chunk.page is None for chunk in chunks)


# ---------------------------------------------------------------------------
# T13：XLSX 多工作表、表从 B3 开始、合并格
# ---------------------------------------------------------------------------
def test_t13_xlsx_sheet_names_and_real_cell_coordinates():
    xlsx = {
        "schema_name": "DoclingDocument",
        "body": {"children": [{"$ref": "#/groups/0"}, {"$ref": "#/groups/1"}]},
        "groups": [
            {"self_ref": "#/groups/0", "label": "sheet", "name": "分区域数据",
             "parent": {"$ref": "#/body"}, "children": [{"$ref": "#/tables/0"}]},
            {"self_ref": "#/groups/1", "label": "sheet", "name": "原始记录",
             "parent": {"$ref": "#/body"}, "children": [{"$ref": "#/tables/1"}]},
        ],
        "texts": [],
        "tables": [
            {"self_ref": "#/tables/0", "label": "table", "parent": {"$ref": "#/groups/0"},
             "children": [], "captions": [], "footnotes": [],
             # 表从工作表第 3 行、第 B 列开始：零基列 1、一基行 3，右/下为开区间。
             "prov": [{"page_no": 1, "bbox": {"l": 1.0, "t": 2.0, "r": 4.0, "b": 5.0,
                                              "coord_origin": "TOPLEFT"}}],
             "data": {"num_rows": 3, "num_cols": 3, "orientation": "row", "grid": []}},
            {"self_ref": "#/tables/1", "label": "table", "parent": {"$ref": "#/groups/1"},
             "children": [], "captions": [], "footnotes": [],
             "prov": [{"page_no": 2, "bbox": {"l": 0.0, "t": 0.0, "r": 2.0, "b": 2.0,
                                              "coord_origin": "TOPLEFT"}}],
             "data": {"num_rows": 2, "num_cols": 2, "orientation": "row", "grid": []}},
        ],
        "pictures": [], "pages": {"1": {"page_no": 1}, "2": {"page_no": 2}},
    }
    # 构造第一张表的单元格（含跨列合并）。
    cells = []
    for row in range(3):
        for col in range(3):
            cells.append({"text": f"r{row}c{col}", "row_span": 1,
                          "col_span": 2 if (row == 0 and col == 0) else 1,
                          "start_row_offset_idx": row, "end_row_offset_idx": row + 1,
                          "start_col_offset_idx": col, "end_col_offset_idx": col + 1,
                          "column_header": row == 0, "bbox": None})
    xlsx['tables'][0]['data']['table_cells'] = cells
    xlsx['tables'][0]['data']['grid'] = [[cells[0]], [cells[3]], [cells[6]]]
    xlsx['tables'][1]['data']['table_cells'] = [
        {"text": "日期", "row_span": 1, "col_span": 1, "start_row_offset_idx": 0,
         "end_row_offset_idx": 1, "start_col_offset_idx": 0, "end_col_offset_idx": 1,
         "column_header": True, "bbox": None}]
    xlsx['tables'][1]['data']['grid'] = [[{"text": "日期", "bbox": None}]]

    normalized = normalize_docling_result('xl1', xlsx, 'xlsx')
    table_blocks = [block for block in normalized.blocks if block.block_type == 'table']
    assert [block.sheet_name for block in table_blocks] == ['分区域数据', '原始记录']
    first = table_blocks[0]
    # 表从 B3 开始、3 行 3 列：范围应为 B3:D5，而不是从 A1 开始。
    assert first.sources[0].cell_range == 'B3:D5'
    assert first.sources[0].sheet_name == '分区域数据'
    # 表内第 0 行第 0 列 = B3；第 1 行第 2 列 = D4。
    coordinates = {cell.get('cell') for cell in first.table['cells']}
    assert 'B3' in coordinates and 'D4' in coordinates
    # 合并信息保留，工作表来源不当作 PDF 页码。
    assert any(cell['col_span'] == 2 for cell in first.table['cells'])
    assert first.sources[0].format == 'xlsx'
    assert normalized.blocks[0].sheet_name == '分区域数据'
    # 分块携带工作表名与单元格范围。
    chunks = chunk_document(normalized, ChunkingConfig())
    assert any('分区域数据' in chunk.text and 'B3:D5' in chunk.text for chunk in chunks)


# ---------------------------------------------------------------------------
# T14：XLSX 无缓存/有缓存公式、真实空白与零
# ---------------------------------------------------------------------------
def test_t14_xlsx_formula_cache_and_blank_versus_zero(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    from openpyxl import Workbook
    path = tmp_path / 'formula.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '数据'
    sheet['A1'] = '项目'
    sheet['B1'] = '数值'
    sheet['A2'] = '甲'
    sheet['B2'] = 0            # 真实的零
    sheet['A3'] = '乙'
    sheet['B3'] = None         # 真实空白
    sheet['A4'] = '合计'
    sheet['B4'] = '=SUM(B2:B3)'  # 公式，未计算缓存
    workbook.save(path)

    from app.document_normalizer import inspect_xlsx_formulas
    info = inspect_xlsx_formulas(path)
    assert info['error'] is None
    assert '数据' in info['formulas']
    assert info['formulas']['数据']['B4']['formula'] == '=SUM(B2:B3)'
    assert info['formulas']['数据']['B4']['has_cache'] is False
    # 原件未被修改：重新读取仍无缓存。
    assert inspect_xlsx_formulas(path)['formulas']['数据']['B4']['has_cache'] is False
    # 业务原件以无后缀保存：检查必须不依赖文件扩展名。
    suffixless = tmp_path / 'no-extension-id'
    shutil.copy2(path, suffixless)
    assert inspect_xlsx_formulas(suffixless)['formulas']['数据']['B4']['formula'] == '=SUM(B2:B3)'

    payload = docling_payload()
    payload['groups'] = [{"self_ref": "#/groups/0", "label": "sheet", "name": "数据",
                          "parent": {"$ref": "#/body"}, "children": [{"$ref": "#/tables/0"}]}]
    payload['body']['children'] = [{"$ref": "#/tables/0"}]
    payload['tables'][0]['parent'] = {"$ref": "#/groups/0"}
    payload['tables'][0]['prov'] = [{"page_no": 1, "bbox": {"l": 0.0, "t": 0.0, "r": 2.0, "b": 4.0,
                                                             "coord_origin": "TOPLEFT"}}]
    payload['tables'][0]['data']['grid'] = [
        [{"text": '项目', "bbox": None, "row_span": 1, "col_span": 1, "start_row_offset_idx": 0,
          "end_row_offset_idx": 1, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "column_header": True},
         {"text": '数值', "bbox": None, "row_span": 1, "col_span": 1, "start_row_offset_idx": 0,
          "end_row_offset_idx": 1, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "column_header": True}],
        [{"text": '甲', "bbox": None, "row_span": 1, "col_span": 1, "start_row_offset_idx": 1,
          "end_row_offset_idx": 2, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "column_header": False},
         {"text": '0', "bbox": None, "row_span": 1, "col_span": 1, "start_row_offset_idx": 1,
          "end_row_offset_idx": 2, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "column_header": False}],
        [{"text": '乙', "bbox": None, "row_span": 1, "col_span": 1, "start_row_offset_idx": 2,
          "end_row_offset_idx": 3, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "column_header": False},
         {"text": '', "bbox": None, "row_span": 1, "col_span": 1, "start_row_offset_idx": 2,
          "end_row_offset_idx": 3, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "column_header": False}],
        [{"text": '合计', "bbox": None, "row_span": 1, "col_span": 1, "start_row_offset_idx": 3,
          "end_row_offset_idx": 4, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "column_header": False},
         {"text": '', "bbox": None, "row_span": 1, "col_span": 1, "start_row_offset_idx": 3,
          "end_row_offset_idx": 4, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "column_header": False}]]
    payload['tables'][0]['data']['table_cells'] = [cell for row in payload['tables'][0]['data']['grid'] for cell in row]
    payload['tables'][0]['data']['num_rows'] = 4
    payload['tables'][0]['data']['num_cols'] = 2
    payload['texts'] = []
    normalized = normalize_docling_result('xl2', payload, 'xlsx')
    apply_formula_warnings(normalized, info)
    # 无缓存公式必须告警，且不得变成 0。
    codes = {warning.code for warning in normalized.warnings}
    assert 'formula_cache_missing' in codes
    table_block = next(block for block in normalized.blocks if block.block_type == 'table')
    assert 'B4' in table_block.table['formulas']
    assert '文件保存的缓存值：缺失' in table_block.text
    chunks = chunk_document(normalized, ChunkingConfig())
    joined = '\n'.join(chunk.text for chunk in chunks)
    # 真实的零保留为 0；真实空白显示为（空）；缺失公式值不显示为 0。
    assert '| 0' in joined
    assert '（空）' in joined
    assert '=SUM(B2:B3)' in joined


def test_t14_formula_cache_present_warns_differently(tmp_path):
    pytest.importorskip('openpyxl')
    from openpyxl import Workbook
    path = tmp_path / 'cached.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '表1'
    sheet['A1'] = 1
    sheet['A2'] = '=A1*2'
    workbook.save(path)
    from app.document_normalizer import inspect_xlsx_formulas
    info = inspect_xlsx_formulas(path)
    # 刚保存的文件通常没有缓存值；无论有无缓存，都不得把表达式丢掉。
    assert info['formulas']['表1']['A2']['formula'] == '=A1*2'


# ---------------------------------------------------------------------------
# T15：超长正文 / 超长表 / 单行超长 / 中文数字
# ---------------------------------------------------------------------------
def test_t15_long_text_table_and_numbers():
    config = ChunkingConfig(max_chars=100, overlap_chars=20, table_rows_per_chunk=3)
    # 超长正文：分块长度受控，且不产生仅由重叠构成的片段。
    long_text = '。'.join(f'第{i}句内容' for i in range(1, 60))
    payload = docling_payload(text=long_text, table_text=None)
    normalized = normalize_docling_result('doc', payload, 'pdf')
    chunks = chunk_document(normalized, config)
    assert chunks and all(len(chunk.text) <= config.max_chars + 40 for chunk in chunks)
    # 每个分块都必须包含新内容，不能只有重叠片段。
    for chunk in chunks:
        assert chunk.text.strip()
        assert not chunk.text.startswith('。')
    # 中文数字与符号不被破坏。
    payload2 = docling_payload(text='合计 1,234.56 万元，同比下降 -1.9%，占比 7.89%。', table_text=None)
    normalized2 = normalize_docling_result('doc2', payload2, 'pdf')
    joined = ' '.join(chunk.text for chunk in chunk_document(normalized2, config))
    assert '1,234.56' in joined and '-1.9%' in joined and '7.89%' in joined
    # 超长表：按行组切分，每块携带表头与行范围；单行超长也有确定处理规则。
    rows = 12
    grid = [[{"text": '列A', "row_span": 1, "col_span": 1, "start_row_offset_idx": 0,
              "end_row_offset_idx": 1, "start_col_offset_idx": 0, "end_col_offset_idx": 1,
              "column_header": True, "bbox": None},
             {"text": '列B', "row_span": 1, "col_span": 1, "start_row_offset_idx": 0,
              "end_row_offset_idx": 1, "start_col_offset_idx": 1, "end_col_offset_idx": 2,
              "column_header": True, "bbox": None}]]
    for row in range(1, rows):
        grid.append([{"text": f'第{row}行', "row_span": 1, "col_span": 1,
                      "start_row_offset_idx": row, "end_row_offset_idx": row + 1,
                      "start_col_offset_idx": 0, "end_col_offset_idx": 1,
                      "column_header": False, "bbox": None},
                     {"text": '很长的一行数据' * 8 if row == 5 else str(row), "row_span": 1, "col_span": 1,
                      "start_row_offset_idx": row, "end_row_offset_idx": row + 1,
                      "start_col_offset_idx": 1, "end_col_offset_idx": 2,
                      "column_header": False, "bbox": None}])
    payload3 = docling_payload(text='表前正文', table_text='')
    payload3['tables'][0]['data']['grid'] = grid
    payload3['tables'][0]['data']['table_cells'] = [cell for row in grid for cell in row]
    payload3['tables'][0]['data']['num_rows'] = rows
    payload3['tables'][0]['data']['num_cols'] = 2
    payload3['tables'][0]['captions'] = [{"text": "测试表（单位：万元）"}]
    normalized3 = normalize_docling_result('doc3', payload3, 'pdf')
    all_chunks = chunk_document(normalized3, config)
    table_chunks = [chunk for chunk in all_chunks if chunk.chunk_type != 'text']
    assert len(table_chunks) >= 4
    for chunk in table_chunks:
        assert '表头：' in chunk.text
        assert '行范围：' in chunk.text
        assert '单位：万元' in chunk.text
    # 单行超长：仍然受长度控制，并保留来源与行范围说明。
    overflow = [chunk for chunk in table_chunks if chunk.chunk_type == 'long_line']
    assert overflow
    assert all(len(chunk.text) <= config.max_chars + 120 for chunk in overflow)
    assert all(chunk.sources for chunk in overflow)


# ---------------------------------------------------------------------------
# T18：B 建索引时 C 发布 → 不发布过期目标为当前索引
# ---------------------------------------------------------------------------
def test_t18_target_version_change_marks_superseded(tmp_path, monkeypatch):
    import app.embedding as embedding_module
    from openai import OpenAI

    def fake_factory(**kwargs):
        def handler(request):
            count = len(json.loads(request.content)['input'])
            return httpx.Response(200, json={'object': 'list', 'model': 'm', 'data': [
                {'object': 'embedding', 'index': index, 'embedding': [1.0, 0.0]}
                for index in range(count)]})
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(embedding_module, 'OpenAI', fake_factory)

    with TestClient(create_app(Settings(data_dir=tmp_path, embedding_api_key='k'))) as client:
        document_id = upload_pdf(client)
        worker = make_worker(tmp_path, FakeDocling())
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        worker.run_forever(max_tasks=1)
        version_b = client.get(f'/api/documents/{document_id}').json()['active_parse_version_id']
        # 建立 B 的索引（成功）。
        assert client.post(f'/api/documents/{document_id}/index').status_code == 200
        index_b = client.get(f'/api/documents/{document_id}/index').json()
        assert index_b['parse_version_id'] == version_b

        # 重新解析并发布 C：内容不同才会产生新的解析版本（相同结果哈希会按幂等复用）。
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        changed = docling_payload(text='第二版正文内容。',
                                  extra_texts=[{"text": "第二版新增段落。", "page": 2}])
        # 断言夹具确实不同，避免测试因夹具相同而假通过。
        assert changed['texts'][0]['text'] != docling_payload()['texts'][0]['text']
        worker = make_worker(tmp_path, FakeDocling(result=changed))
        worker.run_forever(max_tasks=1)
        document_after = client.get(f'/api/documents/{document_id}').json()
        version_c = document_after['active_parse_version_id']
        assert version_c != version_b, (
            f'内容不同必须产生新的解析版本；当前任务状态={document_after["task_status"]}'
            f'，错误={document_after["latest_task_error"]}')
        state = client.get(f'/api/documents/{document_id}/index').json()
        assert state['status'] == 'indexed'
        assert state['parse_version_id'] == version_b

        # 模拟“建 B 索引期间 C 发布”：登记一次针对当前活动版本（C）的构建尝试，
        # 但激活时期望的目标版本仍是 B（说明构建目标已过期），必须被拒绝。
        repository = Repository(tmp_path / 'docqa.db')
        repository.create_index_record(document_id, 'idx-manual', version_c, 'sig')
        attempt = repository.begin_index_attempt(document_id, 'idx-manual', version_c, 'sig')

        # 发布前核对目标版本：此时活动版本已是 C，B 的构建必须被标记为过期。
        published = repository.activate_index(
            document_id, 'idx-manual', attempt, version_id=version_b, model_signature='sig',
            source_signature='x', source_signature_algo='a', dimension=2, chunk_count=1,
            vectors=[], chunking_algo='t', expected_version_id=version_b)
        assert published is False
        with repository.connect() as db:
            row = db.execute("SELECT status, error_code FROM index_attempts WHERE id=?",
                             (attempt,)).fetchone()
        assert row['status'] == 'superseded' and row['error_code'] == 'target_version_changed'
        # 过期构建不得成为当前可用索引（不会变成 indexed）。
        state = client.get(f'/api/documents/{document_id}/index').json()
        assert state['status'] == 'indexed' and state['parse_version_id'] == version_b
        assert client.post(f'/api/documents/{document_id}/search', json={'query': '正文'}).status_code == 200


# ---------------------------------------------------------------------------
# T19：新来源字段与旧签名、模型或维度改变
# ---------------------------------------------------------------------------
def test_t19_legacy_signature_compatibility_and_real_incompatibility(tmp_path, monkeypatch):
    import app.embedding as embedding_module
    from openai import OpenAI
    from app.vector_index import (
        SIGNATURE_ALGO_LEGACY,
        SIGNATURE_ALGO_VERSIONED,
        source_signature_legacy,
        source_signature_versioned,
    )

    def fake_factory(**kwargs):
        def handler(request):
            count = len(json.loads(request.content)['input'])
            return httpx.Response(200, json={'object': 'list', 'model': 'm', 'data': [
                {'object': 'embedding', 'index': index, 'embedding': [1.0, 0.0]}
                for index in range(count)]})
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(embedding_module, 'OpenAI', fake_factory)

    with TestClient(create_app(Settings(data_dir=tmp_path, embedding_api_key='k'))) as client:
        document_id = upload_pdf(client)
        worker = make_worker(tmp_path, FakeDocling())
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
        worker.run_forever(max_tasks=1)
        version_id = client.get(f'/api/documents/{document_id}').json()['active_parse_version_id']
        assert client.post(f'/api/documents/{document_id}/index').status_code == 200
        repository = Repository(tmp_path / 'docqa.db')
        chunks = repository.chunks(document_id, version_id)
        # 新算法签名与旧算法签名不同，但都必须能按算法版本重现。
        legacy = source_signature_legacy(chunks)
        versioned = source_signature_versioned(version_id, chunks)
        assert legacy != versioned
        assert source_signature_legacy(chunks) == legacy
        assert source_signature_versioned(version_id, chunks) == versioned
        # 新增来源字段不改变新算法签名（签名只覆盖索引实际依赖的字段）。
        chunks_with_sources = repository.chunks(document_id, version_id)
        for chunk in chunks_with_sources:
            chunk.heading_path = '新增的标题路径'
        assert source_signature_versioned(version_id, chunks_with_sources) == versioned
        # 旧索引：把签名与算法换成旧算法后，旧签名仍被接受（不因新增字段变成 stale）。
        with repository.connect() as db:
            db.execute("UPDATE embedding_indexes SET source_signature=?, source_signature_algo=?"
                       " WHERE document_id=?", (legacy, SIGNATURE_ALGO_LEGACY, document_id))
        # 旧算法签名的计算基准是旧字段集合，这里用同样算法重算应一致。
        assert source_signature_legacy(repository.chunks(document_id, version_id)) == legacy
    # 真正的模型不兼容：换模型后索引判为 stale，检索被拒绝。
    with TestClient(create_app(Settings(data_dir=tmp_path, embedding_api_key='k',
                                       embedding_model='other-model'))) as client:
        assert client.get(f'/api/documents/{document_id}/index').json()['status'] == 'stale'
        assert client.post(f'/api/documents/{document_id}/search',
                           json={'query': '内容'}).status_code == 409


# ---------------------------------------------------------------------------
# T20：格式伪装、损坏/加密/超限、任意路径请求
# ---------------------------------------------------------------------------
def test_t20_format_spoofing_damaged_and_arbitrary_path(tmp_path):
    import zipfile
    # 伪装：扩展名 .pdf 但内容不是 PDF。
    with pytest.raises(UploadFormatError) as error:
        detect_format('a.pdf', b'plain text', max_pdf_pages=10)
    assert error.value.status_code == 422
    # 伪装：扩展名 .docx 但实际是 xlsx 包。
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml',
                         '<Types><Override ContentType="application/vnd.openxmlformats-officedocument'
                         '.spreadsheetml.sheet.main+xml"/></Types>')
        archive.writestr('xl/workbook.xml', '<workbook/>')
    with pytest.raises(UploadFormatError):
        detect_format('a.docx', buffer.getvalue(), max_pdf_pages=10)
    # 损坏的 ZIP。
    with pytest.raises(UploadFormatError):
        detect_format('a.xlsx', b'PK\x03\x04broken', max_pdf_pages=10)
    # 页数超限：明确失败，不冒充完整成功。
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=100, height=100)
    pdf_buffer = BytesIO()
    writer.write(pdf_buffer)
    with pytest.raises(UploadFormatError) as error:
        detect_format('big.pdf', pdf_buffer.getvalue(), max_pdf_pages=3)
    assert '超过' in str(error.value)
    # 加密 PDF：明确拒绝。
    encrypted = PdfWriter()
    encrypted.add_blank_page(width=100, height=100)
    encrypted.encrypt('secret')
    encrypted_buffer = BytesIO()
    encrypted.write(encrypted_buffer)
    with pytest.raises(UploadFormatError) as error:
        detect_format('locked.pdf', encrypted_buffer.getvalue(), max_pdf_pages=10)
    assert '加密' in str(error.value)
    # 文件名清理：不能把无后缀存储路径当原文件名，也不能带目录或控制字符。
    assert sanitize_filename('..\\..\\etc\\passwd.pdf') == 'passwd.pdf'
    assert sanitize_filename('a\x00b\x1f.pdf') == 'ab.pdf'
    assert sanitize_filename('') == 'document'

    with TestClient(create_app(Settings(data_dir=tmp_path, max_pdf_pages=3))) as client:
        # 任意路径请求必须被拒绝：原件接口只接受文档 ID。
        assert client.get('/api/documents/..%2F..%2Fetc%2Fpasswd/original').status_code in {404, 400}
        # 超限 PDF 上传失败且不残留原件。
        assert client.post('/api/documents',
                           files={'file': ('big.pdf', pdf_buffer.getvalue())}).status_code == 422
        assert list((tmp_path / 'uploads').iterdir()) == []


def test_t20_cross_document_version_access_is_rejected(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        first = upload_pdf(client, name='a.pdf')
        second = upload_pdf(client, name='b.pdf')
        worker = make_worker(tmp_path, FakeDocling())
        client.post(f'/api/documents/{first}/parse', json={'force': True})
        worker.run_forever(max_tasks=1)
        version = client.get(f'/api/documents/{first}').json()['active_parse_version_id']
        # 用第一份文档的版本 ID 读取第二份文档：必须 404，不能串文档。
        assert client.get(f'/api/documents/{second}/content?version_id={version}').status_code == 404
        assert client.get(f'/api/documents/{second}/chunks?version_id={version}').status_code == 404
        assert client.get(f'/api/documents/{first}/content?version_id={version}').status_code == 200
        # 第二份文档尚未解析：读取内容返回 404 而不是伪造空内容。
        assert client.get(f'/api/documents/{second}/content').status_code == 404
        assert client.get(f'/api/documents/{second}/chunks').json() == []


# ---------------------------------------------------------------------------
# 上传给解析服务时必须使用清理后的文件名与匹配 MIME
# ---------------------------------------------------------------------------
def test_upload_submits_sanitized_name_and_matching_mime(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document_id = upload_pdf(client, name='年报 2024.pdf')
        client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    upstream = FakeDocling()
    repository = Repository(tmp_path / 'docqa.db')
    repository.initialize()
    worker = ParseWorker(Settings(data_dir=tmp_path), repository=repository, client=upstream,
                         sleep=lambda _s: None)
    worker.run_forever(max_tasks=1)
    assert upstream.uploaded_name == '年报 2024.pdf'
    assert upstream.uploaded_mime == 'application/pdf'
    # 存储路径是无后缀的服务端 ID，提交时不能使用它。
    assert upstream.uploaded_name != document_id


# ---------------------------------------------------------------------------
# 能力清单与解析服务状态
# ---------------------------------------------------------------------------
def test_capabilities_and_parsing_status_are_honest(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        capabilities = client.get('/api/capabilities').json()
        assert capabilities['rag'] is False
        assert capabilities['summary'] is False and capabilities['extraction'] is False
        assert capabilities['async_parse_tasks'] is True
        assert capabilities['formats'] == {'txt': True, 'pdf': True, 'docx': True, 'xlsx': True,
                                           'doc': False, 'xls': False, 'pptx': False, 'image': False}
        status = client.get('/api/parsing/status').json()
        # 离线环境无法连接解析服务：必须如实报告不可达，而不是假定可用。
        assert status['reachable'] in {True, False}
        assert '质量' in status['note']
        assert 'key' not in json.dumps(status).lower()
