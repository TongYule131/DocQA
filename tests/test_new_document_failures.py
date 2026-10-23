"""新文档发现的乱码入库及页码范围误作数值的回归；仅使用合成原文。"""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.document_normalizer import normalize_docling_result, overall_quality_status
from app.docling_client import DoclingClient
from app.main import create_app
from app.pdf_text_health import pdf_ocr_plan, text_health
from app.rag_answer_contract import validate_task_output
from app.rag_context import EvidenceItem, EvidencePack
from app.rag_validation import ModelOutputError
from app.schemas import SourceLocation
from tests.fixtures.rag_quality.claim_protocol import upgrade
from tests.test_parse_pipeline import FakeDocling, docling_payload, make_worker, upload_pdf


@pytest.mark.parametrize('text', ['控制\x01\x02' * 60, '\ue001\ue002' * 60, '✍✏✑✔✓' * 60])
def test_corrupt_text_is_not_indexable_success(text):
    parsed = normalize_docling_result('doc', docling_payload(text=text), 'pdf')
    assert overall_quality_status(parsed.warnings) == 'invalid'
    assert any(w.code == 'pdf_text_mapping_suspect' and w.page == 1 for w in parsed.warnings)


def test_real_math_symbols_and_bilingual_text_do_not_trigger_garbled_gate():
    text = '最大化利润π=αq−βq²，边界条件为θ≥0、λ∈[0,1]。CO₂排放与∑成本，→为映射。' * 30
    assert not text_health(text)['suspicious']
    parsed = normalize_docling_result('doc', docling_payload(text=text), 'pdf')
    assert overall_quality_status(parsed.warnings) != 'invalid'


def test_native_later_page_corruption_is_detected_but_empty_scan_is_not(monkeypatch):
    import pypdf
    def reader(texts):
        return SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda t=t:t) for t in texts])
    monkeypatch.setattr(pypdf, 'PdfReader', lambda _: reader(['正常正文。', '\x01\x02坏' * 50]))
    plan = pdf_ocr_plan(b'%PDF-synthetic')
    assert plan['force_ocr'] and plan['suspect_pages'] == [2] and plan['inspected_pages'] == 2
    monkeypatch.setattr(pypdf, 'PdfReader', lambda _: reader(['', '正常正文。']))
    assert not pdf_ocr_plan(b'%PDF-synthetic')['force_ocr']


def test_worker_forces_ocr_before_first_post_and_persists_actual_mode(tmp_path, monkeypatch):
    import app.pdf_text_health
    plan = dict(version='pdf-text-health-v1', force_ocr=True, suspect_pages=[1], inspected_pages=1)
    monkeypatch.setattr(app.pdf_text_health, 'pdf_ocr_plan', lambda *a, **k: plan)
    class Recording(FakeDocling):
        forced = False
        def submit(self, *, force_ocr=False, **kwargs):
            self.forced = force_ocr
            return super().submit(**kwargs)
    upstream = Recording()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document = upload_pdf(client)
        task = client.post(f'/api/documents/{document}/parse', json={'force': True}).json()['task']
        worker = make_worker(tmp_path, upstream)
        assert worker.run_task(task['id']) and upstream.submit_calls == 1 and upstream.forced
        version = worker.repository.list_parse_versions(document)[0]
        config = json.loads(version.config_summary)
        assert config['force_ocr'] is True and config['pdf_text_health'] == plan
        saved = worker.repository.get_task(task['id'])
        assert saved.request_summary['pdf_text_health'] == plan and saved.status == 'succeeded'


def test_client_sends_force_ocr_without_changing_global_default():
    requests = []
    def handle(request):
        requests.append(request.content)
        return httpx.Response(200, json={'task_id':'one'})
    with DoclingClient(Settings(), transport=httpx.MockTransport(handle)) as client:
        client.submit(filename='bad.pdf', content=b'%PDF-test', mime='application/pdf', force_ocr=True)
        client.submit(filename='good.pdf', content=b'%PDF-test', mime='application/pdf')
    assert b'name="force_ocr"\r\n\r\ntrue' in requests[0]
    assert b'name="force_ocr"\r\n\r\nfalse' in requests[1]


def page_case(text, pages):
    source = '更名不改变对齐成本的根源。'
    items = [EvidenceItem(reference_id=i, chunk_id=f'c{i}', document_id='d', parse_version_id='v',
                         page=page, text=source, sources=[SourceLocation(format='pdf', page=page)])
             for i, page in enumerate(pages, 1)]
    pack = EvidencePack(items=items, candidates=items, index_id='i', parse_version_id='v', truncated=False, context_chars=100)
    from app.rag_extract_context import assign_unit_options
    for item in items:
        item.segment_policy = 'sentence-segments-v2'
    assign_unit_options(items, [])
    body = dict(status='answered', conclusion=[dict(text=text, refs=list(range(1,len(items)+1)),context=[])],
                explanation=[], clarification_questions=[], missing_points=[],
                evidence_quotes=[dict(ref=i,quote=source) for i in range(1,len(items)+1)],
                answer_parts=[dict(part_id='p1',state='answered',fact_indices=[0],missing_detail='')])
    return pack, upgrade(body, {i:source for i in range(1,len(items)+1)}, strict=True)


@pytest.mark.parametrize('locator,pages', [('第3—4页',[3,4]), ('第1、8页',[1,8]), ('第6、8—9页',[6,8,9]), ('第3页',[3])])
def test_supported_page_addresses_are_not_business_measurements(locator, pages):
    pack, body = page_case(f'更名不能消除对齐税（{locator}）。',pages)
    output = validate_task_output(json.dumps(body,ensure_ascii=False),pack,question='更名的作用？',contract_version='fact-context-parts-v7')
    assert locator in output.conclusion[0].text


@pytest.mark.parametrize('locator,pages', [('第3—5页',[3,5]), ('第9页',[3]), ('第0—3页',[3]), ('第3—999页',[3])])
def test_unquoted_middle_page_and_fabricated_page_are_rejected(locator,pages):
    pack, body = page_case(f'更名不能消除对齐税（{locator}）。',pages)
    with pytest.raises(ModelOutputError,match='page_locator_not_supported'):
        validate_task_output(json.dumps(body,ensure_ascii=False),pack,question='更名的作用？',contract_version='fact-context-parts-v7')


def test_page_locator_exemption_cannot_hide_unsupported_measurement():
    pack, body = page_case('成本为300元（第3—4页）。',[3,4])
    with pytest.raises(ModelOutputError,match='numeric_context_missing'):
        validate_task_output(json.dumps(body,ensure_ascii=False),pack,question='成本是多少？',contract_version='fact-context-parts-v7')


def visible_pdf():
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    for width, height in [(240,320),(320,240)]:
        page = writer.add_blank_page(width=width,height=height)
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),
            NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream = DecodedStreamObject();stream.set_data(b'BT /F1 18 Tf 20 80 Td (Visible policy) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
    out = BytesIO();writer.write(out);writer.close()
    return out.getvalue()


def test_raster_ocr_copy_keeps_visible_pages_and_drops_bad_text_layer():
    from io import BytesIO
    from pypdf import PdfReader
    import pypdfium2 as pdfium
    from PIL import ImageChops,ImageStat
    from app.pdf_raster import rasterize_pdf
    original = visible_pdf()
    rendered = rasterize_pdf(original,max_pages=2,max_bytes=2_000_000)
    reader = PdfReader(BytesIO(rendered))
    assert len(reader.pages)==2 and all(not p.extract_text().strip() for p in reader.pages)
    assert [(float(p.mediabox.width),float(p.mediabox.height)) for p in reader.pages] == [(240,320),(320,240)]
    with pdfium.PdfDocument(original) as a, pdfium.PdfDocument(rendered) as b:
        for n in range(2):
            ap,bp=a[n],b[n];ab,bb=ap.render(scale=2),bp.render(scale=2)
            try:
                left,right=ab.to_pil().convert('RGB'),bb.to_pil().convert('RGB')
                assert max(ImageStat.Stat(ImageChops.difference(left,right)).mean)<3
                left.close();right.close()
            finally:ab.close();bb.close();ap.close();bp.close()


def test_raster_limits_fail_before_unbounded_rendering():
    from app.pdf_raster import rasterize_pdf,PdfRasterError
    original=visible_pdf()
    with pytest.raises(PdfRasterError):rasterize_pdf(original,max_pages=1,max_bytes=2_000_000)
    with pytest.raises(PdfRasterError):rasterize_pdf(original,max_pages=2,max_bytes=2_000_000,max_page_pixels=1)
    with pytest.raises(PdfRasterError):rasterize_pdf(original,max_pages=2,max_bytes=1)


def test_equal_text_embeddings_reuse_vectors_but_keep_all_chunk_identities(tmp_path):
    from tests.test_question_jobs import question_fixture
    from app.schemas import Chunk
    env=question_fixture(tmp_path)
    chunks=[Chunk(id='same-a',document_id='qdoc',page=1,text='相同原文。'),
            Chunk(id='same-b',document_id='qdoc',page=2,text='相同原文。'),
            Chunk(id='other',document_id='qdoc',page=2,text='另一原文。')]
    env.repository.finish_parse('qdoc',2,chunks)
    env.embedding.calls.clear()
    env.rag.index.build('qdoc',rebuild=True)
    assert env.embedding.calls==[['相同原文。','另一原文。']]
    active=env.repository.active_index('qdoc')
    with env.repository.connect() as db:
        rows=db.execute('SELECT chunk_id,vector FROM embedding_vectors_v2 WHERE index_id=?',(active.id,)).fetchall()
    vectors={r['chunk_id']:r['vector'] for r in rows}
    assert set(vectors)=={'same-a','same-b','other'} and vectors['same-a']==vectors['same-b']
    assert env.rag.index.index_info('qdoc')['chunk_count']==3


def test_garbled_conversion_never_replaces_a_previous_readable_version(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document = upload_pdf(client)
        first = client.post(f'/api/documents/{document}/parse',json={'force':True}).json()['task']
        worker = make_worker(tmp_path,FakeDocling())
        worker.run_task(first['id'])
        previous = worker.repository.get(document).active_parse_version_id
        old_chunks = [c.text for c in worker.repository.chunks(document,previous)]
        second = client.post(f'/api/documents/{document}/parse',json={'force':True}).json()['task']
        broken = make_worker(tmp_path,FakeDocling(result=docling_payload(text='✍✏✑✔✓'*100)))
        broken.run_task(second['id'])
        assert broken.repository.get_task(second['id']).error_code == 'quality_invalid'
        assert broken.repository.get(document).active_parse_version_id == previous
        assert [c.text for c in broken.repository.chunks(document,previous)] == old_chunks
