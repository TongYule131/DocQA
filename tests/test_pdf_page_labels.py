"""PDF 地址映射的定向回归；原件、页脚和业务数据全部为合成内容。"""
import hashlib
import json

import pytest
from pypdf import PdfWriter
from pypdf.constants import PageLabelStyle
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from app.chunking import ChunkingConfig, chunk_document
from app.document_normalizer import NormalizedDocument, normalize_docling_result
from app.pdf_page_labels import apply_pdf_page_labels
from app.rag_citation_identity import citation_identity
from app.rag_numeric_roles import numeric_view, source_number_tokens
from app.rag_validation import ModelOutputError
from app.schemas import Block, Chunk, PdfPageLabel, SourceLocation
from app.vector_index import evidence_signature


def pdf(tmp_path, count=4, labels=()):
    target = tmp_path / "synthetic.pdf"
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=600, height=800)
    for first, last, style, start, prefix in labels:
        writer.set_page_label(first, last, style=style, start=start, prefix=prefix)
    writer.write(target)
    return target


def document(footer_labels, *, origin="BOTTOMLEFT", height=800):
    blocks = []
    for page, labels in enumerate(footer_labels, 1):
        source = SourceLocation(format="pdf", page=page, node_ref=f"#/texts/body{page}")
        blocks.append(Block(id=f"b{page}", document_id="d", parse_version_id="v", order_index=len(blocks),
                            block_type="paragraph", text="共同承担费用。", sources=[source]))
        for index, label in enumerate(labels):
            blocks.append(Block(id=f"f{page}-{index}", document_id="d", parse_version_id="v", order_index=len(blocks),
                block_type="page_footer", text=label, sources=[SourceLocation(format="pdf", page=page,
                    node_ref=f"#/texts/footer{page}-{index}", coord_origin=origin, coord_unit="pt",
                    bbox=dict(l=280, r=320, t=height-20 if origin == "TOPLEFT" else 20,
                              b=height-40 if origin == "TOPLEFT" else 40))]))
    return NormalizedDocument(document_id="d", format="pdf", page_count=len(footer_labels), blocks=blocks)


def body_source(doc, page):
    return next(b.sources[0] for b in doc.blocks if b.id == f"b{page}")


def test_old_source_dump_and_evidence_signature_are_byte_compatible():
    source = SourceLocation(format="pdf", page=2, bbox=dict(l=1, t=2, r=3, b=4))
    assert "pdf_page_label" not in source.model_dump()
    assert "pdf_page_label" not in source.model_dump_json()
    chunk = Chunk(id="c", document_id="d", parse_version_id="v", page=2, text="原文", sources=[source])
    old = chunk.model_dump(mode="json")
    old["sources"][0].pop("pdf_page_label", None)
    signature = hashlib.sha256(json.dumps([old], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert evidence_signature([chunk]) == signature
    identity = citation_identity("d", "v", "c", 0, 2, "原文")
    source.pdf_page_label = PdfPageLabel(catalog_label="19", status="catalog_only")
    assert evidence_signature([chunk]) != signature  # 新来源元数据参与固定快照，不能被静默篡改。
    assert citation_identity("d", "v", "c", 0, 2, "原文") == identity


def test_absent_catalog_is_not_pypdf_default_labels(tmp_path):
    doc = document([[], [], [], []])
    report = apply_pdf_page_labels(doc, pdf(tmp_path))
    assert report["catalog_status"] == "absent" and report["mapped_pages"] == 0
    assert body_source(doc, 1).pdf_page_label is None


def test_catalog_supports_roman_decimal_reset_and_prefix_without_printed_claim(tmp_path):
    path = pdf(tmp_path, 6, [(0, 1, PageLabelStyle.LOWERCASE_ROMAN, 1, None),
                           (2, 3, PageLabelStyle.DECIMAL, 19, None),
                           (4, 5, PageLabelStyle.DECIMAL, 1, "App-")])
    doc = document([[] for _ in range(6)])
    report = apply_pdf_page_labels(doc, path)
    labels = [body_source(doc, n).pdf_page_label for n in range(1, 7)]
    assert [m.catalog_label for m in labels] == ["i", "ii", "19", "20", "App-1", "App-2"]
    assert report["status_counts"] == {"catalog_only": 6}
    assert all(m.printed_label is None for m in labels)
    assert "印刷页码未核对" in body_source(doc, 3).label()


@pytest.mark.parametrize("origin", ["BOTTOMLEFT", "TOPLEFT"])
def test_each_footer_is_observed_no_offset_interpolation(tmp_path, origin):
    doc = document([["18"], ["19"], [], ["21"]], origin=origin)
    report = apply_pdf_page_labels(doc, pdf(tmp_path))
    assert report["status_counts"] == {"footer_observed": 2}
    assert body_source(doc, 1).pdf_page_label.printed_label == "18"
    assert body_source(doc, 2).pdf_page_label.printed_label == "19"
    assert body_source(doc, 3).pdf_page_label is None  # 不补 20。
    assert body_source(doc, 4).pdf_page_label is None  # 孤立 21 不足以确认页码。
    assert "PDF 第 2 页" in body_source(doc, 2).label()


def test_catalog_footer_agreement_and_conflict(tmp_path):
    path = pdf(tmp_path, 3, [(0, 2, PageLabelStyle.DECIMAL, 18, None)])
    doc = document([["18"], ["99"], ["20", "21"]])
    report = apply_pdf_page_labels(doc, path)
    assert body_source(doc, 1).pdf_page_label.status == "corroborated"
    assert body_source(doc, 2).pdf_page_label.status == "conflict"
    assert "99" not in body_source(doc, 2).label() and "19" not in body_source(doc, 2).label()
    assert body_source(doc, 3).pdf_page_label is None
    assert report["conflict_pages"] == 1 and report["ambiguous_footer_pages"] == 1
    assert any(w.code == "pdf_page_label_conflict" for w in doc.warnings)


@pytest.mark.parametrize("bad", ["2026年", "成本19元", "19.2%", "<img>", "页脚摘要19", "", "0", "IIV"])
def test_footer_arbitrary_numbers_or_malformed_labels_do_not_map(tmp_path, bad):
    doc = document([[bad], [bad]])
    assert apply_pdf_page_labels(doc, pdf(tmp_path, 2))["mapped_pages"] == 0


def test_footer_geometry_and_original_page_bounds_required(tmp_path):
    doc = document([["18"], ["19"], ["20"], ["21"]])
    for block in doc.blocks:
        if block.block_type == "page_footer":
            block.sources[0].bbox = dict(l=1, r=2, t=400, b=420)
    assert apply_pdf_page_labels(doc, pdf(tmp_path, 2))["mapped_pages"] == 0


def test_unreadable_pdf_has_no_guessed_mapping(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not pdf")
    doc = document([["18"], ["19"]])
    assert apply_pdf_page_labels(doc, path)["catalog_status"] == "unavailable"
    assert all(s.pdf_page_label is None for b in doc.blocks for s in b.sources)


@pytest.mark.parametrize("invalid", ["odd", "empty", "unknown_style", "start_zero", "duplicate", "missing_zero", "unsafe_prefix"])
def test_invalid_catalog_never_becomes_default_position(tmp_path, invalid):
    writer = PdfWriter()
    for _ in range(2): writer.add_blank_page(width=600,height=800)
    entry = DictionaryObject({NameObject('/S'):NameObject('/D')})
    nums = ArrayObject([NumberObject(0),entry])
    if invalid == 'odd': nums.append(NumberObject(1))
    if invalid == 'empty': nums = ArrayObject()
    if invalid == 'unknown_style': entry[NameObject('/S')] = NameObject('/bad')
    if invalid == 'start_zero': entry[NameObject('/St')] = NumberObject(0)
    if invalid == 'duplicate': nums.extend([NumberObject(0),entry])
    if invalid == 'missing_zero': nums[0] = NumberObject(1)
    if invalid == 'unsafe_prefix': entry[NameObject('/P')] = TextStringObject('x\n')
    writer._root_object[NameObject('/PageLabels')] = DictionaryObject({NameObject('/Nums'): nums})
    path = tmp_path / 'invalid.pdf';writer.write(path)
    doc = document([[],[]])
    report = apply_pdf_page_labels(doc,path)
    assert report['catalog_status'] == 'invalid_or_unsupported'
    assert report['mapped_pages'] == 0


def test_catalog_number_tree_kids_supported_without_library_fallback(tmp_path):
    writer = PdfWriter()
    for _ in range(4): writer.add_blank_page(width=600,height=800)
    children = []
    for key, start in [(0,18),(2,31)]:
        child = DictionaryObject({NameObject('/Limits'):ArrayObject([NumberObject(key),NumberObject(key)]),
            NameObject('/Nums'):ArrayObject([NumberObject(key),DictionaryObject({NameObject('/S'):NameObject('/D'),NameObject('/St'):NumberObject(start)})])})
        children.append(writer._add_object(child))
    writer._root_object[NameObject('/PageLabels')] = DictionaryObject({NameObject('/Kids'):ArrayObject(children)})
    path = tmp_path / 'kids.pdf';writer.write(path)
    doc = document([[],[],[],[]]);report=apply_pdf_page_labels(doc,path)
    assert report['catalog_status'] == 'present'
    assert [body_source(doc,n).pdf_page_label.catalog_label for n in range(1,5)] == ['18','19','31','32']


def test_normalize_and_chunk_preserve_mapping_and_do_not_index_footer(tmp_path):
    path = pdf(tmp_path, 2, [(0, 1, PageLabelStyle.DECIMAL, 18, None)])
    nodes = []
    for page in (1, 2):
        for label, text in [("text", "双方共同承担本次投入费用，按约定核实实际成本。"), ("page_footer", str(17+page))]:
            nodes.append(dict(self_ref=f"#/texts/{len(nodes)}", label=label, text=text,
                prov=[dict(page_no=page, bbox=dict(l=20, r=580, t=20, b=40, coord_origin="BOTTOMLEFT"))]))
    payload = dict(pages={str(p): dict(page_no=p, size=dict(width=600, height=800)) for p in (1, 2)}, texts=nodes,
                   body=dict(children=[{"$ref": n["self_ref"]} for n in nodes]))
    doc = normalize_docling_result("d", payload, "pdf", pdf_path=path)
    chunks = chunk_document(doc, ChunkingConfig())
    assert chunks and all(c.text not in {"18", "19"} for c in chunks)
    assert all(s.pdf_page_label.status == "corroborated" for c in chunks for s in c.sources)
    assert doc.stats["page_labels"]["mapped_pages"] == 2
    assert [n["text"] for n in nodes][-1] == "19"  # 不修改上游字面值。


def item(source):
    from app.rag_context import EvidenceItem
    return EvidenceItem(reference_id=1, chunk_id="c", document_id="d", parse_version_id="v", page=source.page,
                        text="共同承担费用。", sources=[source])


@pytest.mark.parametrize("text", ["PDF第2页说明共同承担费用。", "物理第2页说明共同承担费用。",
                                  "印刷第19页说明共同承担费用。", "印刷页码19说明共同承担费用。"])
def test_typed_addresses_validated_from_actual_source_metadata(text):
    source = SourceLocation(format="pdf", page=2, pdf_page_label=PdfPageLabel(
        printed_label="19", footer_node_ref="#/texts/footer", status="footer_observed"))
    assert not source_number_tokens(numeric_view(text, [item(source)], ["共同承担费用。"]))


@pytest.mark.parametrize("status", ["catalog_only", "conflict"])
def test_catalog_or_conflicting_labels_cannot_support_printed_claim(status):
    mapping = (PdfPageLabel(catalog_label="19", status=status) if status == "catalog_only" else
               PdfPageLabel(catalog_label="19", printed_label="20", footer_node_ref="#/f", status=status))
    source = SourceLocation(format="pdf", page=2, pdf_page_label=mapping)
    with pytest.raises(ModelOutputError, match="page_locator_not_supported"):
        numeric_view("印刷第19页说明费用。", [item(source)], ["印刷第19页说明费用。"])
    with pytest.raises(ModelOutputError, match="page_locator_not_supported"):
        numeric_view("第19页说明费用。", [item(source)], ["第19页说明费用。"])


def test_roman_prefix_labels_do_not_supply_business_numbers():
    source = SourceLocation(format="pdf", page=2, pdf_page_label=PdfPageLabel(catalog_label="App-19", status="catalog_only"))
    assert not source_number_tokens(numeric_view("PDF标签App-19说明费用。", [item(source)], ["共同承担费用。"]))
    assert source_number_tokens("印刷第19页的费用19元。") == {"19"}
    source.pdf_page_label = PdfPageLabel(printed_label="iv", footer_node_ref="#/f", status="footer_observed")
    assert not source_number_tokens(numeric_view("印刷第iv页说明费用。", [item(source)], ["共同承担费用。"]))


def test_non_pdf_and_multi_page_source_cannot_have_one_label():
    mapping = PdfPageLabel(catalog_label="19", status="catalog_only")
    with pytest.raises(ValueError, match="page_label_requires_single_pdf_page"):
        SourceLocation(format="txt", page=2, pdf_page_label=mapping)
    with pytest.raises(ValueError, match="page_label_requires_single_pdf_page"):
        SourceLocation(format="pdf", page=2, page_end=3, pdf_page_label=mapping)


def test_printed_address_recall_does_not_match_business_value_or_catalog_only():
    from app.pdf_page_labels import requested_page_slots
    from app.rag_evidence_slots import evidence_slots
    source = SourceLocation(format="pdf", page=2, pdf_page_label=PdfPageLabel(
        printed_label="19", footer_node_ref="#/f", status="footer_observed"))
    text = "双方共同承担本次投入费用，按约定核实实际成本。" * 3
    pool = [dict(chunk_id="correct", text=text, sources=[source.model_dump()]),
            dict(chunk_id="business", text="该设备的采购费用为19元。"*6, sources=[SourceLocation(format="pdf",page=3).model_dump()])]
    request = requested_page_slots("请说明印刷第19页的成本规则。", pool)
    assert request[0]['indices'] == [0]
    slots = evidence_slots("请说明印刷第19页的成本规则。", pool)
    assert next(s for s in slots if s['kind']=='source_page')['candidate_ids'] == ['correct']
    source.pdf_page_label = PdfPageLabel(catalog_label="19",status="catalog_only")
    pool[0]['sources'] = [source.model_dump()]
    assert requested_page_slots("印刷第19页的规则",pool)[0]['indices'] == []
    assert requested_page_slots("PDF标签19的规则",pool)[0]['indices'] == [0]


def test_explicit_page_retrieval_survives_candidate_budget():
    from app.retrieval_planning import plan_retrieval
    text = "双方共同承担本次投入费用，按约定核实实际成本。" * 3
    source = SourceLocation(format="pdf", page=2, pdf_page_label=PdfPageLabel(
        printed_label="19", footer_node_ref="#/f",status="footer_observed"))
    chunks = [Chunk(id=f'c{n}',document_id='d',parse_version_id='v',text=text+str(n),page=n,order_index=n,
                    chunk_type='paragraph',sources=[source if n==2 else SourceLocation(format='pdf',page=n)]) for n in range(1,10)]
    # 第一名向量候选与问题词面相似，但地址并不匹配。
    hit = dict(chunk_id='c1',document_id='d',parse_version_id='v',text=chunks[0].text,page=1,score=1,
               sources=[s.model_dump() for s in chunks[0].sources])
    planned = plan_retrieval('请说明印刷第19页的成本规则。',dict(index_id='i',parse_version_id='v',results=[hit]),
                             chunks,max_candidates=2,max_items=1,fulltext_budget=1200)
    assert planned['results'][0]['chunk_id'] == 'c2'
    assert len(planned['results']) <= 2
    slot = next(s for s in planned['planning']['evidence_slots'] if s['kind']=='source_page')
    assert 'c2' in slot['candidate_ids']


def test_location_digits_cannot_support_cost_via_production_output_contract():
    from tests.test_rag_numeric_roles import task
    mapping = PdfPageLabel(printed_label="19",footer_node_ref="#/f",status="footer_observed")
    sources = [SourceLocation(format='pdf',page=2,pdf_page_label=mapping)]
    assert task('印刷第19页说明双方共同承担费用。',pages=(2,),locations=sources).status == 'answered'
    with pytest.raises(ModelOutputError):
        task('双方承担19元费用。',pages=(2,),locations=sources)


def test_frozen_page_address_replay_has_disjoint_document_splits():
    from scripts.evaluate_page_addresses import evaluate
    report = evaluate(split='all')
    assert report['total'] == report['passed'] == 12 and report['failed'] == report['provider_calls'] == 0
    assert report['split_counts'] == {'dev':4,'calibration':4,'engineering_holdout':4}
    assert report['generated_answer_evaluated'] is False and report['ocr_evaluated'] is False


def test_parse_worker_persists_mapping_in_new_version_and_citations(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.rag import RagService
    from app.rag_context import EvidenceItem, EvidencePack
    from app.repository import Repository
    from tests.test_parse_pipeline import FakeDocling, docling_payload, upload_pdf, worker_for
    path = pdf(tmp_path,2,[(0,1,PageLabelStyle.DECIMAL,18,None)])
    text = '双方共同承担本次投入费用，按约定核实实际成本。' * 3
    payload = docling_payload(text=text,page_count=2,extra_texts=[
        dict(label='page_footer',text='18',page=1),dict(text=text,page=2),dict(label='page_footer',text='19',page=2)])
    data = tmp_path/'isolated';data.mkdir()
    with TestClient(create_app(Settings(data_dir=data))) as client:
        doc_id=upload_pdf(client,content=path.read_bytes())
        response=client.post(f'/api/documents/{doc_id}/parse',json={'force':True})
        assert response.status_code==202
        repository=Repository(data/'docqa.db')
        upstream=FakeDocling(result=payload)
        worker=worker_for(data,repository,upstream)
        worker.run_forever(max_tasks=1)
        assert repository.get_task(response.json()['task']['id']).status=='succeeded'
        version=repository.get(doc_id).active_parse_version_id
        assert repository.get_parse_version(version).result_schema_version=='docqa-normalized-9'
        chunks=repository.chunks(doc_id,version_id=version)
        assert chunks and all(s.pdf_page_label.status=='corroborated' for c in chunks for s in c.sources)
        normalized=json.loads((data/'parse-results'/version/'normalized.json').read_text(encoding='utf-8'))
        assert normalized['stats']['page_labels']['status_counts']=={'corroborated':2}
        original=chunks[0]
        evidence=EvidenceItem(reference_id=1,chunk_id=original.id,document_id=doc_id,parse_version_id=version,
                              page=original.page,text=original.text,sources=original.sources)
        pack=EvidencePack(items=[evidence],candidates=[evidence],index_id='i',parse_version_id=version,
                          truncated=False,context_chars=1000)
        # 引用装配使用同一生产方法；不需要建立向量或调用生成。
        citation=RagService._citations(None,pack,{1:original.text})[0]
        assert citation.sources[0].pdf_page_label==original.sources[0].pdf_page_label
        assert upstream.submit_calls==1 and upstream.result_calls==1
