import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ocr_pdf_candidates import (
    apply_vector_candidates, discover_uncovered, native_reliable,
    tile_boxes, vector_table_candidate,
)


def line(text, box, confidence=.99, **extra):
    return {"text":text,"bbox":box,"confidence":confidence,**extra}


def table():
    texts=[["科目代码","科目名称","金额"],["201","一般公共服务支出","24,200.29"],["202","外交支出",""]]
    return {"id":"vector-0","rows":3,"cols":3,"bbox":[0,0,1,1],
            "cells":[{"id":f"c{r}-{c}","r0":r,"r1":r+1,"c0":c,"c1":c+1,"text":texts[r][c],
                      "bbox":[c/3,r/3,(c+1)/3,(r+1)/3]} for r in range(3) for c in range(3)]}


def test_text_presence_does_not_accept_replacement_characters():
    native={"text":"A"*60+"\ufffd","lines":[{}]}
    assert not native_reliable(native)
    native["text"]="A"*60
    assert native_reliable(native)


def test_tiles_cover_page_and_overlap_without_gold_boxes():
    boxes=tile_boxes()
    assert len(boxes)==4 and boxes[0][0:2]==[0,0] and boxes[-1][2:]==[1,1]
    assert boxes[0][2]>boxes[1][0] and boxes[0][3]>boxes[2][1]


def test_conflicting_high_confidence_digits_do_not_overwrite_baseline():
    original=[line("919",[.1,.1,.3,.2])]
    snapshot=copy.deepcopy(original)
    accepted,records=discover_uncovered(original,[line("616",[.1,.1,.3,.2])])
    assert not accepted and records[0]["reason"]=="overlap_disagreement"
    assert original==snapshot


def test_independent_missing_region_is_retained_with_coordinates():
    accepted,records=discover_uncovered([line("title",[0,0,.5,.1])],[line("2025年5月15日",[.1,.6,.7,.7])])
    assert accepted[0]["bbox"]==[.1,.6,.7,.7]
    assert records[0]["review_required"]


def test_overlapping_tiles_do_not_duplicate_new_region():
    accepted,records=discover_uncovered([],[line("new",[.1,.1,.3,.2]),line("new",[.105,.1,.305,.2])])
    assert len(accepted)==1 and records[1]["reason"]=="already_covered"


def test_edge_and_low_confidence_candidates_abstain():
    accepted,records=discover_uncovered([],[line("broken",[0,0,.3,.1],touches_tile_edge=True),line("unclear",[.1,.6,.3,.7],.7)])
    assert not accepted and {r["reason"] for r in records}=={"tile_edge","low_confidence"}


def test_thin_native_word_box_does_not_duplicate_inside_taller_ocr_box():
    baseline=[line('weight layer',[.224,.124,.284,.128])]
    accepted,records=discover_uncovered(baseline,[line('weight layer',[.222,.119,.286,.131])])
    assert not accepted and records[0]['reason']=='already_covered'


def test_thin_existing_misspelling_is_a_disagreement_not_a_new_line():
    baseline=[line('Clear brown liquia',[.17,.369,.57,.379])]
    accepted,records=discover_uncovered(baseline,[line('Clear brown liquid',[.17,.358,.57,.389])])
    assert not accepted and records[0]['reason']=='overlap_disagreement'


def test_character_boxes_inside_new_formula_are_not_added_as_second_copy():
    baseline=[line(ch,[.1+i*.02,.1,.12+i*.02,.11]) for i,ch in enumerate('F(x)')]
    accepted,records=discover_uncovered(baseline,[line('F(x)',[.095,.095,.185,.14])])
    assert not accepted and records[0]['review_required']


def test_vector_grid_retains_blank_and_does_not_convert_it_to_zero():
    candidate,reason=vector_table_candidate(table())
    assert reason=="vector_grid_candidate" and candidate["header_end"]==1
    assert candidate["cells"][-1]["text"]==""
    assert all(c["column_header"]==(c["r0"]==0) for c in candidate["cells"])


def test_vector_grid_rejects_missing_cell_and_overlap():
    value=table();value["cells"].pop()
    assert vector_table_candidate(value)[1]=="incomplete_grid"
    value=table();value["cells"].append(copy.deepcopy(value["cells"][0]))
    assert vector_table_candidate(value)[1]=="overlapping_cells"


def test_header_is_not_guessed_when_no_body_boundary_exists():
    value=table()
    for c in value["cells"]:c["text"]="label"
    assert vector_table_candidate(value)[1]=="ambiguous_header"


def test_vector_replacement_preserves_document_reading_order():
    old=table();old.update(id="old",order=7)
    prediction={"tables":[old],"units":[{"id":"paragraph","kind":"text","order":3,"text":"earlier"}],"review_required":False}
    native={"text":"原生文字"*20,"lines":[{}],"tables":[table()]}
    candidate=apply_vector_candidates(prediction,native)
    assert candidate["tables"][0]["order"]==7
    assert candidate["units"][0]==prediction["units"][0]
    assert candidate["review_required"] and prediction["tables"][0]["id"]=="old"


def test_vector_grid_abstains_on_missing_matching_table():
    prediction={"tables":[],"units":[],"review_required":False}
    native={"text":"native"*20,"lines":[{}],"tables":[table()]}
    result=apply_vector_candidates(prediction,native)
    assert result["tables"]==[] and not result["vector_records"][0]["accepted"]


def test_runner_checks_source_hash_and_prevents_parent_escape(tmp_path):
    from scripts.run_ocr_pdf_benchmark import checked
    inputs=tmp_path/'inputs';inputs.mkdir()
    source=inputs/'page.png';source.write_bytes(b'original')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    assert checked(inputs,'page.png',digest)==source
    source.write_bytes(b'changed')
    with pytest.raises(ValueError,match='Input changed'):
        checked(inputs,'page.png',digest)
    outside=tmp_path/'gold.json';outside.write_bytes(b'answer')
    with pytest.raises(ValueError,match='escaped'):
        checked(inputs,'../gold.json',hashlib.sha256(outside.read_bytes()).hexdigest())


def test_page_route_cli_defaults_to_no_inference_or_output(tmp_path):
    inputs=tmp_path/'inputs';inputs.mkdir()
    (inputs/'plan.json').write_text(json.dumps({'pages':[{'id':'scan','split':'dev'}]}),encoding='utf-8')
    output=tmp_path/'output'
    result=subprocess.run([sys.executable,str(Path(__file__).parents[1]/'scripts/run_ocr_pdf_benchmark.py'),
        '--inputs',str(inputs),'--output',str(output),'--profile','page-routed','--split','dev'],
        capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)['mode']=='plan_only' and not output.exists()
