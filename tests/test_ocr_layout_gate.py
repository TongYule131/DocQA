import copy
import pytest
from scripts.ocr_layout_gate import assess


def report(cer=.01,review=0):
    metrics={'cer':cer,'critical_recall':1,'order_accuracy':1,'order_correct':2,'table_accuracy':1,'table_cells_correct':2,'review_required_pages':review}
    summary={'completed':True,'pages':[{'id':'one','elapsed_seconds':1},{'id':'two','elapsed_seconds':1}],'gpu':{'peak_bytes':1024**3}}
    return {'split':'holdout','overall':metrics,'pages':[{'id':'one','regions':[]},{'id':'two','regions':[]}],
            'by_source_kind':{'real_typeset_rasterized':{'cer':cer},'real_low_resolution_scan':{'cer':cer}},'inference_summary':summary}


def test_cached_route_cost_is_not_presented_as_full_pipeline_speed():
    base=report(.02);candidate=report(.01)
    stage=copy.deepcopy(base['inference_summary'])
    gate=assess(base,candidate,stage,stage)
    assert gate['actual_staged_median_latency_ratio']==3
    assert not gate['accepted_for_default']
    assert not gate['checks']['actual_staged_median_latency_le_2x']


def test_good_average_cer_cannot_mask_structural_failure_or_unreviewed_changes():
    base=report(.02);candidate=report(.01,1);candidate['overall']['table_accuracy']=.8
    short=copy.deepcopy(base['inference_summary'])
    for row in short['pages']:row['elapsed_seconds']=.1
    gate=assess(base,candidate,short,short)
    assert not gate['checks']['table_cells_ge_98pct']
    assert not gate['checks']['no_pending_text_disagreements']
    assert gate['checks']['actual_staged_median_latency_le_2x']
    candidate['pages'].pop()
    with pytest.raises(ValueError):assess(base,candidate,short,short)
