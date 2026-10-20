import pytest
from scripts.ocr_table_grid import canonical_table
from scripts.ocr_layout_metrics import score_page,aggregate
from tests.test_ocr_layout_metrics import sample,prediction


def test_sparse_empty_is_not_missing_and_never_becomes_zero():
    table={'id':'t','bbox':[0,0,1,1],'order':0,'rows':1,'cols':2,
           'cells':[{'r0':0,'r1':1,'c0':0,'c1':1,'text':'25%','column_header':False}]}
    result=canonical_table(table)
    assert len(result['cells'])==2 and result['implicit_empty_cells']==1
    assert result['cells'][1]['text']==''
    assert len(table['cells'])==1


def test_merged_header_is_not_filled_with_fake_blank_children():
    table={'rows':1,'cols':2,'cells':[{'r0':0,'r1':1,'c0':0,'c1':2,'text':'合并','column_header':True}]}
    assert len(canonical_table(table)['cells'])==1


def test_adapter_cannot_fix_lost_nonempty_values_or_wrong_shape():
    actual=prediction();actual['tables'][0]['cells']=actual['tables'][0]['cells'][:1]
    actual['tables'][0]=canonical_table(actual['tables'][0])
    score=aggregate([score_page(sample(),actual)])
    assert score['table_cells_correct']==2  # 合并表头+原本空格；25%仍缺失。
    actual['tables'][0]['rows']=1
    with pytest.raises(ValueError):canonical_table(actual['tables'][0])
