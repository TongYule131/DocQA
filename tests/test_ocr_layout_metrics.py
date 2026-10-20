import copy
from scripts.ocr_layout_metrics import score_page,aggregate,region_text


def sample():
    return {'id':'page','source_kind':'test','split':'dev','regions':[
        {'id':'a','bbox':[0,0,1,.2],'expected':'第一段','critical_spans':['第一段'],'order_anchor':'第一段'},
        {'id':'b','bbox':[0,.3,1,.5],'expected':'第二段','critical_spans':[],'order_anchor':'第二段'}],
        'order_edges':[['a','b'],['b','table:t']],
        'tables':[{'id':'t','bbox':[0,.6,1,1],'rows':2,'cols':2,'cells':[
             {'r0':0,'r1':1,'c0':0,'c1':2,'text':'合并表头','column_header':True},
             {'r0':1,'r1':2,'c0':0,'c1':1,'text':'25%','column_header':False},
             {'r0':1,'r1':2,'c0':1,'c1':2,'text':'','column_header':False}]}]}


def prediction():
    gt=sample()
    return {'status':'success','units':[
        {'kind':'text','bbox':[0,.01,1,.15],'text':'第一段','order':0},
        {'kind':'text','bbox':[0,.31,1,.45],'text':'第二段','order':1}],
        'ocr_lines':[{'bbox':[0,.01,1,.15],'text':'第一段'},{'bbox':[0,.31,1,.45],'text':'第二段'}],
        'tables':[{'id':'pred','bbox':[0,.6,1,1],'order':2,'rows':2,'cols':2,'cells':copy.deepcopy(gt['tables'][0]['cells'])}]}


def test_missing_blocks_keep_all_denominators_and_blank_cells_are_not_zero():
    result=aggregate([score_page(sample(),None)])
    assert result['cer']==1 and result['order_correct']==0
    assert result['table_cells_total']==3 and result['table_cells_correct']==0
    good=prediction();good['tables'][0]['cells'][-1]['text']='0'
    result=aggregate([score_page(sample(),good)])
    assert result['blank_cells_correct']==0 and result['table_cells_correct']==2


def test_right_text_wrong_row_or_merged_header_does_not_pass():
    actual=prediction();assert aggregate([score_page(sample(),actual)])['table_cells_correct']==3
    actual['tables'][0]['cells'][0]['c1']=1
    actual['tables'][0]['cells'][1]['r0']=0
    result=aggregate([score_page(sample(),actual)])
    assert result['table_cells_correct']==1


def test_order_is_output_order_not_ground_truth_coordinates():
    actual=prediction();actual['units'][0]['order']=2;actual['units'][1]['order']=0
    result=aggregate([score_page(sample(),actual)])
    assert result['cer']==0 and result['order_correct']==1


def test_shared_paragraph_uses_real_anchor_offsets_only():
    actual=prediction();actual['units']=[{'kind':'text','bbox':[0,0,1,.5],'text':'第二段 第一段','order':0}]
    result=score_page(sample(),actual)
    assert not result['edges'][0]['correct']
    actual['units'][0]['text']='第一段 第二段'
    assert score_page(sample(),actual)['edges'][0]['correct']


def test_physical_line_assembly_does_not_count_as_layout_order():
    lines=[{'bbox':[.6,.10,.9,.13],'text':'7/24/90'},{'bbox':[.1,.105,.3,.135],'text':'Date'}]
    assert region_text(lines,[0,0,1,1])=='Date 7/24/90'
