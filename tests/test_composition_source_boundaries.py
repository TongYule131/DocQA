"""闭句选项和显式阶段锚点；原文与可见范围不变，不能借活动名冒充阶段身份。"""
import json
import pytest
from tests.test_rag_composition import pack
from app.rag_extract_context import assign_unit_options
from app.rag_composition import refine_pack,unit_catalog,plan_request,validate_plan
from app.rag_validation import ModelOutputError


def test_refinement_omits_partial_next_sentence_without_reading_hidden_tail():
    # 长句跨过实际可见小段边界；短句会按既有规则整句装入，并非隐藏范围。
    p=pack('完整句子说明第一项。下一项只露出开头，'+'后面仍有重要条件'*20+'。')
    original=p.items[0];end=original.text.index('，')+1
    original.read_ranges=[[0,end]];assign_unit_options(p.items,[])
    refined=refine_pack(p);units=unit_catalog(refined)
    assert units and all('下一项' not in u['passages'][0]['text'] for u in units)
    assert refined.items[0].text==original.text and refined.items[0].read_ranges==[[0,end]]
    assert '重要条件' not in ''.join(p['text'] for u in units for p in u['passages'])


def test_explicit_stage_anchor_rejects_activity_label_even_when_substring_is_in_source():
    question='比较两个阶段的实践任务变化'
    p=refine_pack(pack('1. 入门阶段：认识参数 本课时体验参数变化。2. 进阶阶段：理解网络 本课时连接多个单元。'))
    request=json.loads(plan_request(p,question));options=request['stage_anchor_options'];assert len(options)>=2
    anchors=[{'ref':o['ref'],'unit':o['unit']} for o in options[:2]]
    value={'blocks':[{'role':'comparison','focus':'比较任务','part_ids':['p1'],'groups':[
        {'label':'参数','label_source':anchors[0],'units':[anchors[0]]},
        {'label':'网络','label_source':anchors[1],'units':[anchors[1]]}]}],'missing_part_ids':[]}
    with pytest.raises(ModelOutputError,match='composition_binding_invalid'):validate_plan(value,p,question)
    for group in value['blocks'][0]['groups']:group['label']='@label:1'
    valid=validate_plan(value,p,question,resolve_label_choices=True)
    assert '入门阶段' in valid['blocks'][0]['groups'][0]['label'] and '进阶阶段' in valid['blocks'][0]['groups'][1]['label']
