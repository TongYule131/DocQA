"""代码审查发现的恢复与OCR实验边界；仅临时库和模型边界桩。"""
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_ocr_pdf_benchmark
from scripts import run_ocr_layout
from scripts.ocr_layout_gate import assess
from scripts.ocr_pdf_candidates import apply_vector_candidates
from tests.test_ocr_layout_gate import report
from tests.test_ocr_pdf_candidates import table
from tests.test_question_jobs import QUESTION, question_fixture, submit
from tests.test_question_worker import worker


@pytest.mark.parametrize('field,value',[('order_index',-999),('chunk_type','table')])
def test_resume_rejects_changed_retrieval_metadata_before_generation(tmp_path,field,value):
    env=question_fixture(tmp_path);job,_=submit(env)
    old=env.service.store.claim('old',60,job_id=job['id'])
    sequence=env.service.store.reserve(job['id'],old['lease_token'],'embedding')
    cached=env.rag.index.search_snapshot('qdoc',QUESTION,8,snapshot=json.loads(old['snapshot_json']))
    cached['results'][0][field]=value
    assert env.service.store.save_checkpoint(job['id'],old['lease_token'],role='retrieval',payload=cached,sequence_no=sequence)
    with env.repository.connect() as db:
        db.execute("UPDATE question_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(job['id'],))
    assert worker(env).run_job(job['id'])
    result=env.service.get_job(job['id'])
    assert result['status']=='failed' and not result['result_id']
    assert result['requests_used']==1 and len(env.embedding.calls)==1 and not env.model.calls


def test_vector_table_replacement_keeps_units_and_cells_in_one_identity():
    old=table();old.update(id='original-table',order=7)
    cells=[{**c,'kind':'cell','table_id':old['id'],'order':7} for c in old['cells']]
    prediction={'tables':[old],'units':[{'id':old['id'],'kind':'table','bbox':old['bbox'],'order':7,'text':''},
        *cells,{'id':'after','kind':'text','order':8,'text':'after table'}],'review_required':False}
    native={'text':'原生文字'*20,'lines':[{}],'tables':[table()]}
    actual=apply_vector_candidates(prediction,native)
    assert actual['tables'][0]['id']=='original-table'
    assert actual['units'][-1]['id']=='after'
    newcells=[u for u in actual['units'] if u['kind']=='cell']
    assert all(c['table_id']=='original-table' and c['order']==7 for c in newcells)
    assert newcells==actual['tables'][0]['cells']


@pytest.mark.parametrize('pages',[[],[{'id':'same','split':'dev'},{'id':'same','split':'dev'}],
                                [{'id':'../escape','split':'dev'}]])
def test_benchmark_rejects_invalid_page_selection_before_loading_models(tmp_path,pages):
    inputs=tmp_path/'inputs';inputs.mkdir()
    (inputs/'plan.json').write_text(json.dumps({'pages':pages}),encoding='utf-8')
    out=tmp_path/'outputs'
    result=subprocess.run([sys.executable,str(Path(__file__).parents[1]/'scripts/run_ocr_pdf_benchmark.py'),
        '--inputs',str(inputs),'--output',str(out),'--profile','page-routed','--split','dev'],capture_output=True,text=True)
    assert result.returncode!=0 and not out.exists()


def test_supplement_rejects_baseline_from_different_frozen_input(tmp_path,monkeypatch):
    inputs=tmp_path/'inputs';inputs.mkdir();outputs=tmp_path/'outputs'
    image=inputs/'scan.png';image.write_bytes(b'image-bytes')
    native=inputs/'native.json';native.write_text(json.dumps({'text':'native '*30,'lines':[{}]}),encoding='utf-8')
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    model_digest=hashlib.sha256(b'model').hexdigest()
    source={'id':'one','split':'dev','image':'scan.png','image_sha256':digest(image),
            'native_candidate':'native.json','native_candidate_sha256':digest(native),'width':100,'height':100}
    plan={'pages':[source],'config':{'native_min_chars':40,'supplement_confidence':.92,
          'max_supplement_lines':40,'gpu_device_cap_gib':6.5},'models':{'small_det':model_digest,'small_rec':model_digest}}
    (inputs/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
    baseline=outputs/'page-routed/dev';baseline.mkdir(parents=True)
    (baseline/'one.json').write_text(json.dumps({'id':'one','profile':'page-routed','status':'success',
        'input_sha256':'stale-input','image_sha256':source['image_sha256'],'width':100,'height':100,
        'picture_boxes':[],'ocr_lines':[],'units':[],'tables':[]}),encoding='utf-8')
    class Memory:
        def __init__(self):self.samples=[1]
        def sample(self):pass
        def close(self):return {'peak_bytes':1}
    real_read=Path.read_bytes
    def model_bytes(path):
        return b'model' if path.as_posix().startswith('/cache/') else real_read(path)
    monkeypatch.setattr(Path,'read_bytes',model_bytes)
    monkeypatch.setitem(sys.modules,'run_ocr_profile',SimpleNamespace(GpuMemory=Memory))
    monkeypatch.setitem(sys.modules,'run_ocr_layout',SimpleNamespace(export_page=None,make_reader=None))
    monkeypatch.setitem(sys.modules,'cv2',SimpleNamespace())
    monkeypatch.setattr(run_ocr_pdf_benchmark.importlib.metadata,'version',lambda _: 'test')
    monkeypatch.setattr(sys,'argv',['run','--inputs',str(inputs),'--output',str(outputs),
        '--profile','routed-supplement','--split','dev','--execute'])
    assert run_ocr_pdf_benchmark.main()==1
    assert not (outputs/'routed-supplement/dev/one.json').exists()
    summary=json.loads((outputs/'routed-supplement/dev/summary.json').read_text(encoding='utf-8'))
    assert not summary['completed'] and 'baseline' in summary['message'].lower()


def test_gate_rejects_dropped_annotation_despite_same_page_ids():
    baseline=report(.02);candidate=report(.01)
    baseline['pages'][0]['regions']=[{'id':'critical-region','reference_characters':10,
                                    'critical':[{'span':'25%','found':False}]}]
    stage=copy.deepcopy(baseline['inference_summary'])
    with pytest.raises(ValueError,match='标注'):
        assess(baseline,candidate,stage,stage)


def test_gate_rejects_inference_scope_smaller_than_scored_pages():
    baseline=report(.02);candidate=report(.01)
    baseline['inference_summary']['pages'].pop()
    stage=copy.deepcopy(baseline['inference_summary'])
    with pytest.raises(ValueError,match='推理页'):
        assess(baseline,candidate,stage,stage)


def test_partial_docling_conversion_is_not_exported_as_complete_inference(monkeypatch):
    cell=SimpleNamespace(text='partial text',confidence=1,rect=SimpleNamespace(to_bounding_box=lambda:None))
    doc=SimpleNamespace(pages={1:SimpleNamespace(size=SimpleNamespace(width=100,height=100))},iterate_items=lambda:[])
    result=SimpleNamespace(document=doc,status=SimpleNamespace(value='partial_success'),
                           pages=[SimpleNamespace(parsed_page=SimpleNamespace(textline_cells=[cell]))])
    monkeypatch.setattr(run_ocr_layout,'normalize_box',lambda *_: [0,0,1,.1])
    with pytest.raises(ValueError,match='conversion'):
        run_ocr_layout.export_page(result,{'id':'one','width':100,'height':100})
