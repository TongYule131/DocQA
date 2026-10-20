from scripts.ocr_routing import accept_candidate,choose_regions,route_model

CONFIG={'confidence_trigger':.92,'small_text_pixels':14,'max_regions_per_page':2,
        'han_fraction_medium':.45,'candidate_min_confidence':.90,'min_length_ratio':.5,'max_length_ratio':1.8}


def test_router_preserves_latin_and_uses_page_language_only_for_numeric_units():
    assert route_model({'text':'GOVERN'},'总体国家安全观',CONFIG)=='small'
    assert route_model({'text':'27,745.86'},'单位：国家统计局 金额单位：万元',CONFIG)=='medium'
    assert route_model({'text':'25.0%'},'AVERAGE WEEKLY MOVEMENT',CONFIG)=='small'
    assert route_model({'text':'学生的学习认知'},'AI',CONFIG)=='medium'
    assert route_model({'text':'123'},'',CONFIG)=='small'


def test_selection_uses_actual_boxes_and_does_not_invent_blank_cell_risks():
    page={'height':1000,'units':[
       {'id':'blank','kind':'cell','bbox':[0,.5,.2,.6],'text':'','order':0},
       {'id':'first','kind':'text','bbox':[0,0,.5,.2],'text':'PRINT','order':1},
       {'id':'number','kind':'cell','bbox':[0,.3,.5,.4],'text':'25%','order':2},
       {'id':'parent-table','kind':'table','bbox':[0,0,1,1],'text':'','order':3}],
       'ocr_lines':[{'bbox':[0,0,.3,.01],'confidence':.7}]}
    selected=choose_regions(page,CONFIG)
    assert [r['id'] for r in selected]==['first','number']
    assert selected[0]['reasons']==['low_confidence','small_text']
    assert selected[1]['reasons']==['table_number']


def test_candidate_filter_does_not_treat_more_text_or_empty_baseline_as_truth():
    assert accept_candidate('25%','125%',.98,CONFIG)[0]  # 实验候选仍可能错，必须由后续gold关卡拦截。
    assert not accept_candidate('25%','全年增长125%',.99,CONFIG)[0]
    assert not accept_candidate('','0',.99,CONFIG)[0]
    assert not accept_candidate('25%','25%',.89,CONFIG)[0]
