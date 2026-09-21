# 基础回归测试：使用临时存储和内存生成的样本，不依赖外部模型服务。
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

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
    # 共用上传辅助函数，返回服务端生成的文档 ID。
    response = client.post('/api/documents', files={'file': ('sample.txt', text.encode(), 'text/plain')})
    assert response.status_code == 201
    return response.json()['id']


def test_document_lifecycle_and_persistence(tmp_path):
    # 验证上传、解析、重复解析及重新创建应用后读取持久化数据的完整流程。
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/api/health').json()['status'] == 'ok'
        document_id = upload(client)
        parsed = client.post(f'/api/documents/{document_id}/parse')
        assert parsed.status_code == 200
        assert parsed.json()['status'] == 'parsed'
        chunks = client.get(f'/api/documents/{document_id}/chunks').json()
        assert chunks[0]['page'] == 1
        assert chunks[0]['document_id'] == document_id
        assert '可追溯' in chunks[0]['text']
        assert client.post(f'/api/documents/{document_id}/parse').json() == parsed.json()
    with TestClient(create_app(settings)) as client:
        assert len(client.get('/api/documents').json()) == 1
        assert client.get(f'/api/documents/{document_id}/chunks').json() == chunks


def test_upload_validation_and_cleanup(client, tmp_path):
    # 非支持格式、空文件和超限文件均应被拒绝，且不能残留文件或元数据。
    assert client.post('/api/documents', files={'file': ('a.exe', b'x')}).status_code == 415
    assert client.post('/api/documents', files={'file': ('a.txt', b'')}).status_code == 422
    assert client.post('/api/documents', files={'file': ('a.txt', b'x' * (1024 * 1024 + 1))}).status_code == 413
    assert client.get('/api/documents').json() == []
    assert list((tmp_path / 'uploads').iterdir()) == []


def test_intelligence_explicitly_unavailable(client):
    # 区分未解析的前置条件错误与未接模型的能力错误，并校验空白问题。
    document_id = upload(client)
    path = f'/api/documents/{document_id}'
    assert client.post(path + '/summary').status_code == 409
    client.post(path + '/parse')
    for endpoint in ['summary', 'extract', 'questions']:
        assert client.post(path + '/' + endpoint, json={'question': '核心结论？'}).status_code == 503
    assert client.post(path + '/questions', json={'question': '  '}).status_code == 422
    assert client.get('/api/capabilities').json()['rag'] is False


def test_invalid_pdf_and_scanned_pdf(client):
    # 用损坏字节和无文本层空白页验证失败路径；空白页不等于真实扫描样本。
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)
    for content, message in [(b'not a pdf', 'PDF 无法解析'), (buffer.getvalue(), 'OCR')]:
        doc = client.post('/api/documents', files={'file': ('file.pdf', content)}).json()
        response = client.post(f"/api/documents/{doc['id']}/parse")
        assert response.status_code == 422
        assert message in response.json()['detail']
        assert client.get(f"/api/documents/{doc['id']}").json()['status'] == 'failed'


def test_missing_document(client):
    # 各文档入口对不存在的 ID 都应返回 404。
    assert client.get('/api/documents/missing').status_code == 404
    assert client.post('/api/documents/missing/parse').status_code == 404
    assert client.get('/api/documents/missing/chunks').status_code == 404


def test_text_pdf_keeps_page_source(client):
    # 在内存构造两页文本 PDF，验证真实 PDF 解析能保留内容与页码关联。
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
    document_id = response.json()['id']
    assert client.post(f'/api/documents/{document_id}/parse').json()['page_count'] == 2
    chunks = client.get(f'/api/documents/{document_id}/chunks').json()
    assert [chunk['page'] for chunk in chunks] == [1, 2]
    assert 'Second page conclusion' in chunks[1]['text']


def test_chunk_overlap_and_source():
    # 用短字符串直观验证滑动步长、页尾停止条件及分块不跨页的规则。
    chunks = chunk_pages('doc1', [Page(number=3, text='abcdefghijk'), Page(number=4, text='xyz')], size=5, overlap=2)
    assert [c.text for c in chunks] == ['abcde', 'defgh', 'ghijk', 'xyz']
    assert [c.page for c in chunks] == [3, 3, 3, 4]
    assert all(c.document_id == 'doc1' for c in chunks)
