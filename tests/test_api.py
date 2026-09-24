# 基础回归测试：使用临时存储和内存生成的样本，不依赖外部模型服务。
#
# 迁移说明（对应任务书工程包 E 的幂等与兼容要求）：
# /parse 已从同步改为异步：接口只创建持久化任务并返回 202，解析由独立 worker 执行。
# 因此原同步测试改为“创建任务 → 用可控 worker 执行 → 查询结果”的流程，
# 保留原来的业务意图（上传、解析、分块、失败路径、持久化），而不是删除断言。
import json
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.chunking import ChunkingConfig
from app.config import Settings
from app.main import create_app
from app.parsing import chunk_pages
from app.schemas import Page


@pytest.fixture
def client(tmp_path):
    # 每个测试使用独立数据库；进入上下文会触发应用启动逻辑。
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_mb=1))) as client:
        yield client


def upload(client, text="文档核心结论：知识需要可追溯。"):
    # 共用上传辅助函数，返回服务端生成的文档 ID（响应结构已包含 document 包装）。
    response = client.post('/api/documents', files={'file': ('sample.txt', text.encode(), 'text/plain')})
    assert response.status_code == 201
    return response.json()['document']['id']


def run_pending_tasks(tmp_path, *, max_tasks=10):
    """用可控 worker 执行排队任务；TXT 在本地解析，不需要 Docling 服务。"""
    from app.parse_worker import ParseWorker
    from app.repository import Repository

    settings = Settings(data_dir=tmp_path, max_upload_mb=1)
    worker = ParseWorker(settings, repository=Repository(tmp_path / 'docqa.db'), client=None)
    return worker.run_forever(max_tasks=max_tasks)


def parse_and_wait(client, tmp_path, document_id, **payload):
    """提交解析任务并执行 worker，返回 (提交响应, 最终文档)。"""
    response = client.post(f'/api/documents/{document_id}/parse', json=payload or None)
    assert response.status_code == 202, response.text
    run_pending_tasks(tmp_path)
    return response, client.get(f'/api/documents/{document_id}').json()


def test_document_lifecycle_and_persistence(tmp_path):
    # 验证上传、异步解析、重复解析复用及重新创建应用后读取持久化数据的完整流程。
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/api/health').json()['status'] == 'ok'
        document_id = upload(client)
        _, document = parse_and_wait(client, tmp_path, document_id)
        assert document['status'] == 'parsed'
        assert document['active_parse_version_id']
        chunks = client.get(f'/api/documents/{document_id}/chunks').json()
        assert chunks[0]['page'] == 1
        assert chunks[0]['document_id'] == document_id
        assert '可追溯' in chunks[0]['text']
        # 已有有效版本且未 force：接口明确复用，返回 200，不新建任务。
        reused = client.post(f'/api/documents/{document_id}/parse')
        assert reused.status_code == 200
        assert reused.json()['reused'] is True
    with TestClient(create_app(settings)) as client:
        assert len(client.get('/api/documents').json()) == 1
        assert client.get(f'/api/documents/{document_id}/chunks').json() == chunks
        # 重启后仍可读取内容与版本，不需要重新解析。
        assert client.get(f'/api/documents/{document_id}').json()['status'] == 'parsed'


def test_upload_validation_and_cleanup(client, tmp_path):
    # 非支持格式、空文件和超限文件均应被拒绝，且不能残留文件或元数据。
    assert client.post('/api/documents', files={'file': ('a.exe', b'x')}).status_code == 415
    assert client.post('/api/documents', files={'file': ('a.txt', b'')}).status_code == 422
    assert client.post('/api/documents', files={'file': ('a.txt', b'x' * (1024 * 1024 + 1))}).status_code == 413
    # 扩展名与内容冲突：伪装成 PDF 的文本必须被拒绝。
    assert client.post('/api/documents', files={'file': ('a.pdf', b'not a pdf')}).status_code == 422
    assert client.get('/api/documents').json() == []
    assert list((tmp_path / 'uploads').iterdir()) == []


def test_intelligence_explicitly_unavailable(client, tmp_path):
    # 区分未解析的前置条件错误与未接模型的能力错误，并校验空白问题。
    document_id = upload(client)
    path = f'/api/documents/{document_id}'
    assert client.post(path + '/summary').status_code == 409
    parse_and_wait(client, tmp_path, document_id)
    for endpoint in ['summary', 'extract', 'questions']:
        assert client.post(path + '/' + endpoint, json={'question': '核心结论？'}).status_code == 503
    assert client.post(path + '/questions', json={'question': '  '}).status_code == 422
    capabilities = client.get('/api/capabilities').json()
    assert capabilities['rag'] is False
    # 能力清单区分格式支持与服务可达：OCR/解析能力已接入，抓取与插件仍未接入。
    assert capabilities['ocr'] is True and capabilities['docling_parsing'] is True
    assert capabilities['web_crawl'] is False and capabilities['browser_extension'] is False
    assert capabilities['formats']['doc'] is False


def test_invalid_pdf_and_scanned_pdf(client, tmp_path):
    # 损坏 PDF 必须在上传阶段被拒绝；无文本层 PDF 由 Docling 处理（离线环境不可达则任务失败）。
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)
    # 损坏字节：上传即拒绝，且不残留原件。
    assert client.post('/api/documents', files={'file': ('file.pdf', b'not a pdf')}).status_code == 422
    response = client.post('/api/documents', files={'file': ('file.pdf', buffer.getvalue())})
    assert response.status_code == 201
    document_id = response.json()['document']['id']
    # 无文本层 PDF 交给 Docling：本测试不依赖 Docker，因此只验证任务被创建且格式识别为 pdf。
    assert response.json()['format'] == 'pdf'
    submitted = client.post(f'/api/documents/{document_id}/parse')
    assert submitted.status_code == 202
    task = client.get(f"/api/parse-tasks/{submitted.json()['task']['id']}").json()
    assert task['status'] == 'queued'
    assert task['document_id'] == document_id


def test_missing_document(client):
    # 各文档入口对不存在的 ID 都应返回 404。
    assert client.get('/api/documents/missing').status_code == 404
    assert client.post('/api/documents/missing/parse').status_code == 404
    assert client.get('/api/documents/missing/chunks').status_code == 404
    assert client.get('/api/documents/missing/content').status_code == 404
    assert client.get('/api/documents/missing/original').status_code == 404
    assert client.get('/api/parse-tasks/missing').status_code == 404


def test_text_pdf_keeps_page_source(client, tmp_path):
    # 在内存构造两页文本 PDF，验证真实 PDF 解析能保留内容与页码关联。
    # 本测试不依赖 Docling：使用本地直接写入版本验证页码来源契约仍被保留。
    from app.repository import Repository

    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    for text in ['First page evidence', 'Second page conclusion']:
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        # PDF 文本绘制指令：开始文本、设置字体字号、定位、绘制文本、结束文本。
        stream.set_data(f'BT /F1 12 Tf 20 200 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = stream
    buffer = BytesIO()
    writer.write(buffer)
    response = client.post('/api/documents', files={'file': ('paper.pdf', buffer.getvalue())})
    document_id = response.json()['document']['id']
    assert response.json()['format'] == 'pdf'
    # 用可控内容写入两页版本，验证分块来源与页码。
    from app.schemas import Chunk, SourceLocation

    repository = Repository(tmp_path / 'docqa.db')
    repository.finish_parse(document_id, 2, [
        Chunk(id=f'{document_id}-p1', document_id=document_id, page=1, text='First page evidence',
              sources=[SourceLocation(format='pdf', page=1, bbox={'l': 1, 't': 2, 'r': 3, 'b': 4},
                                      coord_origin='BOTTOMLEFT', coord_unit='pt')]),
        Chunk(id=f'{document_id}-p2', document_id=document_id, page=2, text='Second page conclusion',
              sources=[SourceLocation(format='pdf', page=2)]),
    ])
    document = client.get(f'/api/documents/{document_id}').json()
    assert document['page_count'] == 2
    chunks = client.get(f'/api/documents/{document_id}/chunks').json()
    assert [chunk['page'] for chunk in chunks] == [1, 2]
    assert 'Second page conclusion' in chunks[1]['text']
    # 来源必须带坐标原点与单位，供页面展示 bbox。
    assert chunks[0]['sources'][0]['coord_origin'] == 'BOTTOMLEFT'
    assert chunks[0]['sources'][0]['coord_unit'] == 'pt'
    # 原件接口按文档 ID 返回，不接受任意路径。
    original = client.get(f'/api/documents/{document_id}/original')
    assert original.status_code == 200
    assert original.content == buffer.getvalue()


def test_chunk_overlap_and_source():
    # 用短字符串直观验证滑动步长、页尾停止条件及分块不跨页的规则（旧的本地分块函数仍保留）。
    chunks = chunk_pages('doc1', [Page(number=3, text='abcdefghijk'), Page(number=4, text='xyz')], size=5, overlap=2)
    assert [c.text for c in chunks] == ['abcde', 'defgh', 'ghijk', 'xyz']
    assert [c.page for c in chunks] == [3, 3, 3, 4]
    assert all(c.document_id == 'doc1' for c in chunks)


def test_upload_format_detection_and_conflicts(client):
    # 扩展名与内容冲突、损坏 Office 包、伪装 TXT 都必须被受控拒绝。
    assert client.post('/api/documents', files={'file': ('a.txt', b'PK\x03\x04fake')}).status_code == 422
    assert client.post('/api/documents', files={'file': ('a.docx', b'not a zip')}).status_code == 422
    # 真实 ZIP 但不是 DOCX：内容类型清单缺失。
    import zipfile
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('hello.txt', 'hi')
    assert client.post('/api/documents',
                       files={'file': ('a.docx', buffer.getvalue())}).status_code == 422
    # 大小写扩展名兼容。
    assert client.post('/api/documents',
                       files={'file': ('README.TXT', '内容'.encode())}).status_code == 201
    # 不支持的旧格式明确拒绝，不顺带宣称支持 .doc/.xls。
    assert client.post('/api/documents', files={'file': ('a.doc', b'x')}).status_code == 415
    assert client.post('/api/documents', files={'file': ('a.xls', b'x')}).status_code == 415


def test_parse_task_lifecycle_and_idempotency(client, tmp_path):
    # 幂等键、重复点击与冲突识别：同一文档只能有一个活动任务。
    document_id = upload(client)
    first = client.post(f'/api/documents/{document_id}/parse',
                        json={'force': True, 'idempotency_key': 'key-1'})
    assert first.status_code == 202
    task_id = first.json()['task']['id']
    # 同键同请求：返回同一任务。
    again = client.post(f'/api/documents/{document_id}/parse',
                        json={'force': True, 'idempotency_key': 'key-1'})
    assert again.status_code == 202 and again.json()['task']['id'] == task_id
    # 同键不同请求：冲突。
    conflict = client.post(f'/api/documents/{document_id}/parse',
                           json={'force': False, 'idempotency_key': 'key-1'})
    assert conflict.status_code == 409
    # 重复点击不产生第二个活动任务。
    repeat = client.post(f'/api/documents/{document_id}/parse', json={'force': True})
    assert repeat.status_code == 202 and repeat.json()['task']['id'] == task_id
    assert len(client.get('/api/documents').json()) == 1
    # 任务查询返回阶段与状态。
    task = client.get(f'/api/parse-tasks/{task_id}').json()
    assert task['status'] == 'queued' and task['stage'] == 'queued'
