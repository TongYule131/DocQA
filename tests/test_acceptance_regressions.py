"""独立验收反例：使用临时库和模拟网络，断言任务书要求，不修改业务代码。"""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from app.config import Settings
from app.repository import Repository, now_iso
from app.schemas import Document, Chunk, ParseTask, Block, SourceLocation
from app.vector_index import DocumentIndex
from app.embedding import EmbeddingError
from app.parse_worker import ParseWorker
from app.docling_client import DoclingClient
from app.document_normalizer import normalize_docling_result, NormalizedDocument
from app.chunking import chunk_document, ChunkingConfig


class LocalEmbedding:
    signature = 'review-model'
    settings = SimpleNamespace(embedding_api_key='mock-only')
    fail = False
    def embed(self, texts):
        if self.fail:
            raise EmbeddingError(502, '验收注入：网关失败')
        return [[1.0, 0.0] for _ in texts]


def payload(text='新正文', status='success'):
    structure = {'body': {'children': [{'$ref': '#/texts/0'}]},
                 'texts': [{'self_ref': '#/texts/0', 'parent': {'$ref': '#/body'},
                            'label': 'text', 'text': text,
                            'prov': [{'page_no': 1, 'bbox': {'l': 1, 't': 10, 'r': 10, 'b': 1,
                                                          'coord_origin': 'BOTTOMLEFT'}}]}],
                 'tables': [], 'groups': [], 'pictures': [], 'pages': {'1': {'page_no': 1}}}
    return {'status': status, 'document': {'json_content': structure, 'md_content': text}}


def setup(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repo = Repository(tmp_path/'docqa.db'); repo.initialize()
    repo.create(Document(id='review-doc', filename='sample.pdf', size=1,
                         format='pdf', created_at=now_iso(), status='uploaded'))
    (tmp_path/'uploads').mkdir(); (tmp_path/'uploads'/'review-doc').write_bytes(b'fake')
    repo.finish_parse('review-doc', 1, [Chunk(id='old-chunk', document_id='review-doc',
                                          page=1, text='旧正文')])
    emb = LocalEmbedding(); index = DocumentIndex(repo, emb); index.build('review-doc')
    return settings, repo, emb, index


def task(repo):
    return repo.create_task(ParseTask(id=uuid4().hex, document_id='review-doc',
                                     status='queued', stage='queued', created_at=now_iso(), updated_at=now_iso()))


def client(settings, result, calls):
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == 'POST':
            return httpx.Response(200, json={'task_id': 'remote-review', 'task_status': 'pending'})
        if '/status/' in request.url.path:
            return httpx.Response(200, json={'task_status': 'success'})
        return httpx.Response(200, json=result)
    return DoclingClient(settings, transport=httpx.MockTransport(handler))


def test_successful_reparse_keeps_old_index_and_content(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    old = repo.get('review-doc').active_parse_version_id
    t = task(repo)
    with client(settings, payload(), []) as c:
        ParseWorker(settings, repo, c).run_task(t.id)
    assert repo.get_task(t.id).status == 'succeeded'
    assert repo.get_parse_version(old) is not None, '成功重新解析不应删除旧版本'
    assert index.search('review-doc', '旧正文', 1)['is_old_version'] is True


def test_failed_rebuild_keeps_old_index_queryable(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    emb.fail = True
    with pytest.raises(EmbeddingError):
        index.build('review-doc', rebuild=True)
    emb.fail = False
    assert index.status('review-doc')['status'] == 'indexed', '候选构建失败不应使旧索引变为 failed'
    assert index.search('review-doc', '旧正文', 1)['results']


def test_saved_normalized_blocks_match_published_version(tmp_path):
    """结果文件也必须能按同一块编号追溯数据库中的来源，不能保存绑定前的临时编号。"""
    settings, repo, _, _ = setup(tmp_path)
    t = task(repo)
    with client(settings, payload(), []) as c:
        ParseWorker(settings, repo, c).run_task(t.id)
    version_id = repo.get('review-doc').active_parse_version_id
    saved = json.loads((tmp_path/'parse-results'/version_id/'normalized.json').read_text(encoding='utf-8'))
    with repo.connect() as db:
        rows = db.execute('SELECT id, parse_version_id FROM blocks WHERE parse_version_id=?', (version_id,)).fetchall()
    assert {(b['id'], b['parse_version_id']) for b in saved['blocks']} == {(r['id'], r['parse_version_id']) for r in rows}
    assert saved['blocks'] and all(b['parse_version_id'] == version_id for b in saved['blocks'])


@pytest.mark.parametrize('status', ['partial_success', None, 'unknown'])
def test_non_success_conversion_must_not_publish(tmp_path, status):
    settings, repo, emb, index = setup(tmp_path)
    old = repo.get('review-doc').active_parse_version_id
    t = task(repo)
    with client(settings, payload(status=status), []) as c:
        ParseWorker(settings, repo, c).run_task(t.id)
    assert repo.get('review-doc').active_parse_version_id == old, f'{status} 被发布成成功版本'


def test_worker_restart_resumes_known_remote_id(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    t = task(repo); claimed = repo.claim_next_task('old-worker', 60, task_id=t.id)
    repo.update_task_progress(t.id, claimed.lease_token, stage='waiting_upstream', upstream_task_id='remote-review')
    with repo.connect() as db:
        db.execute("UPDATE parse_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (t.id,))
    calls = []
    with client(settings, payload(), calls) as c:
        ParseWorker(settings, repo, c).run_forever(max_tasks=1)
    assert repo.get_task(t.id).status == 'succeeded', '保存了远端 ID 的中断任务应恢复领取'
    assert not any(method == 'POST' for method, _ in calls)


def test_expired_submitting_task_not_automatically_posted_again(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    t = task(repo); claimed = repo.claim_next_task('old-worker', 60, task_id=t.id)
    repo.update_task_progress(t.id, claimed.lease_token, stage='submitting')
    with repo.connect() as db:
        db.execute("UPDATE parse_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (t.id,))
    # 模拟另一个已经运行的 worker，按循环直接领取过期任务，不重新 initialize。
    taken = repo.claim_next_task('existing-worker', 60, task_id=t.id)
    calls = []
    if taken:
        with client(settings, payload(), calls) as c:
            ParseWorker(settings, repo, c).process_task(taken)
    assert not any(method == 'POST' for method, _ in calls), '提交中断且无远端 ID，禁止自动重新 POST'


def text_node(i, text, parent='#/body', children=None, label='text'):
    return {'self_ref': f'#/texts/{i}', 'label': label, 'text': text,
            'parent': {'$ref': parent}, 'children': children or [], 'prov': []}


def test_nested_heading_children_preserve_order():
    d = {'body': {'children': [{'$ref': '#/texts/0'}]},
         'texts': [text_node(0, '标题', children=[{'$ref': '#/texts/1'}, {'$ref': '#/texts/2'}], label='title'),
                   text_node(1, '第一段', '#/texts/0'), text_node(2, '第二段', '#/texts/0')]}
    n = normalize_docling_result('d', d, 'docx')
    assert [b.text for b in n.blocks] == ['标题', '第一段', '第二段']


def test_group_children_stay_before_following_sibling():
    d = {'body': {'children': [{'$ref': '#/groups/0'}, {'$ref': '#/texts/1'}]},
         'groups': [{'self_ref': '#/groups/0', 'label': 'list', 'children': [{'$ref': '#/texts/0'}]}],
         'texts': [text_node(0, '列表项', '#/groups/0'), text_node(1, '列表后的结论')]}
    n = normalize_docling_result('d', d, 'docx')
    assert [b.text for b in n.blocks] == ['列表项', '列表后的结论']


def test_table_caption_refs_and_footnotes_in_chunks():
    d = {'body': {'children': [{'$ref': '#/tables/0'}]},
         'texts': [text_node(0, '2024年数据（万元）', '#/tables/0', label='caption'),
                   text_node(1, '仅包含境内业务', '#/tables/0', label='footnote')],
         'tables': [{'self_ref': '#/tables/0', 'label': 'table', 'prov': [],
                     'captions': [{'$ref': '#/texts/0'}], 'footnotes': [{'$ref': '#/texts/1'}],
                     'children': [{'$ref': '#/texts/0'}, {'$ref': '#/texts/1'}],
                     'data': {'num_rows': 2, 'num_cols': 1, 'table_cells': [
                         {'text': '金额', 'start_row_offset_idx': 0, 'start_col_offset_idx': 0,
                          'row_span': 1, 'col_span': 1, 'column_header': True},
                         {'text': '100', 'start_row_offset_idx': 1, 'start_col_offset_idx': 0,
                          'row_span': 1, 'col_span': 1}]}}]}
    n = normalize_docling_result('d', d, 'docx')
    chunks = chunk_document(n, ChunkingConfig())
    assert all('2024年数据（万元）' in c.text and '仅包含境内业务' in c.text for c in chunks)


def test_separate_page_chunks_have_corresponding_sources():
    n = NormalizedDocument(document_id='d', format='pdf', page_count=2)
    n.blocks = [Block(id=f'b{i}', document_id='d', parse_version_id='', order_index=i,
                      block_type='paragraph', text=label*80, sources=[SourceLocation(format='pdf', page=i+1)])
                for i, label in enumerate(['甲', '乙'])]
    chunks = chunk_document(n, ChunkingConfig(max_chars=100, overlap_chars=0))
    second = next(c for c in chunks if '乙' in c.text)
    assert second.page == 2 and {s.page for s in second.sources} == {2}


def test_long_table_context_respects_chunk_size():
    n = NormalizedDocument(document_id='d', format='docx', page_count=0)
    n.blocks = [Block(id='t', document_id='d', parse_version_id='', order_index=0,
                      block_type='table', text='', sources=[], table={
                          'captions': ['标题'*300], 'num_rows': 2, 'num_cols': 1,
                          'cells': [{'row': 0, 'col': 0, 'text': '金额', 'column_header': True},
                                    {'row': 1, 'col': 0, 'text': '100'}]})]
    # 口径自身超限时必须受控拒绝，不能截断条件或发送超长块。
    with pytest.raises(ValueError, match='分块容量'):
        chunk_document(n, ChunkingConfig(max_chars=200, overlap_chars=0))


def test_index_in_flight_keeps_old_search_and_rejects_second_build(tmp_path):
    """在真实构建方法执行期间查询和重建，不能依靠构建完成后的状态推测。"""
    settings, repo, emb, index = setup(tmp_path)
    old_id = index.status('review-doc')['index_id']
    original = emb.embed
    seen = []

    def during_build(texts):
        emb.embed = original  # 查询也要向量化，避免递归进入故障注入。
        seen.append(index.search('review-doc', '旧正文', 1)['index_id'])
        with pytest.raises(EmbeddingError) as exc:
            index.build('review-doc', rebuild=True)
        assert exc.value.status_code == 409
        return original(texts)

    emb.embed = during_build
    index.build('review-doc', rebuild=True)
    assert seen == [old_id]
    assert index.status('review-doc')['index_id'] != old_id


def test_late_candidate_does_not_override_current_version(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    old_id = index.status('review-doc')['index_id']
    original = emb.embed

    def publish_c(texts):
        emb.embed = original
        repo.finish_parse('review-doc', 1, [Chunk(id='c', document_id='review-doc', page=1, text='版本C')])
        return original(texts)

    emb.embed = publish_c
    with pytest.raises(EmbeddingError):
        index.build('review-doc', rebuild=True)
    assert index.search('review-doc', '旧正文', 1)['index_id'] == old_id
    assert index.status('review-doc')['attempts'][0]['status'] == 'superseded'


def test_model_change_builds_independent_index(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    old_id = index.status('review-doc')['index_id']
    emb.signature = 'different-model'
    assert index.status('review-doc')['status'] == 'stale'
    index.build('review-doc', rebuild=True)
    assert index.search('review-doc', '旧正文', 1)['index_id'] != old_id
    assert repo.index_vectors(old_id)


def test_cancel_stops_claiming_and_retry_reuses_remote_id(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings, repo, emb, index = setup(tmp_path)
    t = task(repo); held = repo.claim_next_task('worker', 60, task_id=t.id)
    repo.update_task_progress(t.id, held.lease_token, stage='waiting_upstream', upstream_task_id='remote-review')
    repo.request_cancel(t.id)
    calls = []
    with client(settings, payload(), calls) as c:
        ParseWorker(settings, repo, c).process_task(repo.get_task(t.id))
    assert repo.get_task(t.id).status == 'needs_attention'
    assert repo.claim_next_task('worker', 60) is None
    with TestClient(create_app(settings)) as web:
        resumed = web.post(f'/api/parse-tasks/{t.id}/retry').json()
    assert resumed['upstream_task_id'] == 'remote-review'
    with client(settings, payload(), calls) as c:
        ParseWorker(settings, repo, c).run_task(resumed['id'])
    assert repo.get_task(resumed['id']).status == 'succeeded'
    assert not any(method == 'POST' for method, _ in calls)


@pytest.mark.parametrize('field', ['error_message', 'failure_reason'])
def test_upstream_task_failure_never_exposes_raw_details(tmp_path, field):
    """使用真实客户端而非替代 wait_for_result 的假对象，防止脱敏断言绕过生产路径。"""
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings, repo, _, _ = setup(tmp_path)
    t = task(repo)
    private = 'Bearer regression-secret C:/private/trace <script>alert(1)</script>'
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'task_id': 'safe-remote-id'})
        return httpx.Response(200, json={'task_status': 'failure', field: private})
    with DoclingClient(settings, transport=httpx.MockTransport(handler)) as c:
        ParseWorker(settings, repo, c).run_task(t.id)
    failed = repo.get_task(t.id)
    assert failed.status == 'failed'
    assert failed.error_code == 'docling_task_failed'
    with TestClient(create_app(settings)) as api:
        result = api.get(f'/api/parse-tasks/{t.id}')
    assert result.status_code == 200
    for value in ['regression-secret', 'C:/private', '<script>']:
        assert value not in result.text
        assert value not in failed.model_dump_json()


def test_expired_owner_cannot_renew_or_publish_progress(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    t = task(repo); held = repo.claim_next_task('old', 60, task_id=t.id)
    with repo.connect() as db:
        db.execute("UPDATE parse_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (t.id,))
    assert not repo.renew_lease(t.id, held.lease_token, 60)
    assert not repo.update_task_progress(t.id, held.lease_token, stage='publishing')
    assert not repo.finish_task(t.id, held.lease_token, status='succeeded', stage='done')


def test_repeat_result_preserves_task_result_link(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    versions = []
    for _ in range(2):
        t = task(repo)
        with client(settings, payload(), []) as c:
            ParseWorker(settings, repo, c).run_task(t.id)
        result = repo.get_task(t.id)
        assert result.status == 'succeeded' and result.result_version_id
        versions.append(result.result_version_id)
    assert versions[0] == versions[1]


def test_v1_upgrade_failure_rolls_back_then_preserves_index(tmp_path, monkeypatch):
    """真实构造 v1 库，验证升级失败、备份和重复升级，不修改正式库。"""
    import sqlite3
    from app import migrations
    from tests.test_migration_and_versions import build_legacy_db
    path = tmp_path/'docqa.db'
    build_legacy_db(path, provider_signature='sig')
    monkeypatch.setattr(migrations, 'SCHEMA_VERSION', 1)
    repo = Repository(path); repo.initialize()
    before = repo.index_vector_map('legacy-index-legacy-doc')
    with repo.connect() as db:
        rows_before = [tuple(r) for r in db.execute('SELECT * FROM embedding_vectors_v2')]
    monkeypatch.setattr(migrations, 'SCHEMA_VERSION', 2)
    migrations.MigrationHook.register(2, lambda: (_ for _ in ()).throw(RuntimeError('injected')))
    try:
        with pytest.raises(migrations.MigrationError):
            repo.initialize()
    finally:
        migrations.MigrationHook.clear()
    with sqlite3.connect(path) as db:
        assert migrations.detect_version(db) == 1
        assert db.execute('SELECT * FROM embedding_vectors_v2').fetchall() == rows_before
    result = repo.initialize()
    assert result['applied'] == [2] and result['backup']
    assert repo.initialize()['applied'] == []
    with repo.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM embedding_vectors_v2')] == rows_before
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_candidate_crash_expires_without_invalidating_old_index(tmp_path):
    settings, repo, emb, index = setup(tmp_path)
    old_id = index.status('review-doc')['index_id']
    version = repo.get('review-doc').active_parse_version_id
    repo.create_index_record('review-doc', 'crashed-index', version, emb.signature)
    attempt = repo.begin_index_attempt('review-doc', 'crashed-index', version, emb.signature)
    with repo.connect() as db:
        db.execute("UPDATE embedding_indexes SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id='crashed-index'")
    assert index.search('review-doc', '旧正文', 1)['index_id'] == old_id
    index.build('review-doc', rebuild=True)
    with repo.connect() as db:
        assert db.execute('SELECT status FROM index_attempts WHERE id=?', (attempt,)).fetchone()[0] == 'failed'
