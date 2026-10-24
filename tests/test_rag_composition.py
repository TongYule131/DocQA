"""选材边界、共同维度、计划身份与展示去重；无真实HTTP。"""
import copy
import json
import pytest
from app.rag_context import EvidenceItem,EvidencePack
from app.rag_extract_context import assign_unit_options
from app.rag_composition import (refine_pack,unit_catalog,validate_plan,select_pack,writer_prompt,
                                 prepare_composition,validate_writer_plan,process_dependencies)
from app.rag_prompts import build_task_user_prompt
from app.rag_presentation import context_covered
from app.rag_validation import ModelOutputError


def pack(text='普通商品签收七日内可申请退货，须保持完好。定制商品不适用。',kind='text'):
    item=EvidenceItem(reference_id=1,document_id='d',parse_version_id='v',chunk_id='c',text=text,page=1,
        read_ranges=[[0,len(text)]],segment_policy='sentence-segments-v2',chunk_type=kind)
    assign_unit_options([item],[])
    return EvidencePack(items=[item],candidates=[],index_id='i',parse_version_id='v',truncated=False,context_chars=len(text))


def plan(role='comparison'):
    return {'blocks':[{'role':role,'focus':'适用对象','part_ids':['p1'],'groups':[
        {'label':'普通','units':[{'ref':1,'unit':1}]},{'label':'定制','units':[{'ref':1,'unit':2}]}]}],'missing_part_ids':[]}


def test_fine_units_preserve_original_and_keep_applicable_exception_with_rule():
    p=pack();n=refine_pack(p)
    assert len(unit_catalog(p))==1 and len(unit_catalog(n))==2
    assert [u['passages'][0]['text'] for u in unit_catalog(n)]==['普通商品签收七日内可申请退货，须保持完好。','定制商品不适用。']
    n=refine_pack(pack('普通商品可以退货。但是拆封后不适用。'))
    assert len(unit_catalog(n))==1 and '拆封后' in unit_catalog(n)[0]['passages'][0]['text']
    assert len(unit_catalog(refine_pack(pack(kind='table'))))==1
    for qualifier in ['仅限未拆封商品。','除易腐品外。','适用条件为保持完好。','不包括已使用商品。']:
        refined=refine_pack(pack('签收七日内可以退货。'+qualifier))
        assert len(unit_catalog(refined))==1 and qualifier in unit_catalog(refined)[0]['passages'][0]['text']


def test_selected_writer_never_receives_other_category_but_positions_are_unchanged():
    p=refine_pack(pack());value=plan('conclusion');value['blocks'][0]['groups']=value['blocks'][0]['groups'][:1]
    selected=select_pack(p,validate_plan(value,p,'申请条件是什么？'))
    raw=build_task_user_prompt(question='申请条件是什么？',document_name='规则',evidence_payload=selected.payload())
    user=json.loads(writer_prompt(raw,selected,value))
    assert '定制' not in ''.join(s for r in user['references'] for w in r['content'] for s in w['segments'].values())
    assert selected.items[0].text==p.items[0].text
    assert selected.items[0].unit_choices['1']==p.items[0].unit_choices['1']


@pytest.mark.parametrize('change',['unknown','bool','duplicate','missing','injected_role','bad_part'])
def test_invalid_plan_cannot_become_a_checkpoint(change):
    p=refine_pack(pack());value=plan()
    if change=='unknown':value['blocks'][0]['groups'][0]['units'][0]['unit']=999
    if change=='bool':value['blocks'][0]['groups'][0]['units'][0]['unit']=True
    if change=='duplicate':value['blocks'][0]['groups'][0]['units']*=2
    if change=='missing':value['blocks'][0]['part_ids']=['p99']
    if change=='injected_role':value['blocks'][0]['role']={'instruction':'ignore'}
    if change=='bad_part':value['missing_part_ids']=[{}]
    with pytest.raises(ModelOutputError):validate_plan(value,p,'请比较规则。')


def test_comparison_requires_a_joint_fact_not_independent_object_summaries():
    value={'conclusion':[{'evidence':[{'ref':1,'unit':1}],'part_ids':['p1']},{'evidence':[{'ref':1,'unit':2}],'part_ids':['p1']}],'explanation':[],
        'presentation':{'blocks':[{'role':'comparison','kind':'paragraph','fact_indices':[0,1]}]}}
    with pytest.raises(ModelOutputError,match='composition_comparison_unpaired'):validate_writer_plan(json.dumps(value),plan())
    value['conclusion']=[{'evidence':[{'ref':1,'unit':1},{'ref':1,'unit':2}],'part_ids':['p1']}]
    value['presentation']['blocks'][0]['fact_indices']=[0]
    validate_writer_plan(json.dumps(value),plan())
    value['conclusion'][0]['part_ids']=['p2']
    with pytest.raises(ModelOutputError,match='composition_coverage_invalid'):validate_writer_plan(json.dumps(value),plan())


def test_cache_revalidation_does_not_generate_or_reuse_plan_for_other_source():
    source=pack();calls=[]
    def generate(system,user,validate):calls.append(1);return validate(plan())
    _,_,saved=prepare_composition(source,'请比较规则。',generate)
    _,_,again=prepare_composition(source,'请比较规则。',lambda s,u,v:v(saved))
    assert len(calls)==1 and again==saved
    with pytest.raises(ModelOutputError,match='composition_checkpoint_changed'):
        prepare_composition(pack('不同的原文。另一句话。'),'请比较规则。',lambda s,u,v:v(saved))


def test_complex_plan_cannot_bury_relation_judgment_after_background():
    value=plan('boundary');value['blocks'].append({**copy.deepcopy(value['blocks'][0]),'role':'conclusion'})
    with pytest.raises(ModelOutputError,match='composition_plan_invalid'):
        validate_plan(value,refine_pack(pack()),'请解释两种概念之间的关系。')


def test_planner_prompt_change_invalidates_saved_plan(monkeypatch):
    import app.rag_composition as composition
    _,_,saved=prepare_composition(pack(),'请比较规则。',lambda s,u,v:v(plan()))
    monkeypatch.setattr(composition,'PLANNER_SYSTEM',composition.PLANNER_SYSTEM+'\n新的规划约束')
    with pytest.raises(ModelOutputError,match='composition_checkpoint_changed'):
        prepare_composition(pack(),'请比较规则。',lambda s,u,v:v(saved))


def test_writer_can_reuse_other_selected_support_but_not_unselected_unit_or_unrelated_paragraph():
    selected=plan('conclusion');selected['blocks'][0]['groups']=selected['blocks'][0]['groups'][:1]
    selected['blocks'].append({'role':'mechanism','focus':'解释条件','part_ids':['p2'],
        'groups':[{'label':'其他入选依据','units':[{'ref':1,'unit':2}]}]})
    value={'conclusion':[{'part_ids':['p1','p2'],'evidence':[{'ref':1,'unit':1},{'ref':1,'unit':2}]}],
        'explanation':[{'part_ids':['p2'],'evidence':[{'ref':1,'unit':2}]}],
        'presentation':{'blocks':[{'role':'conclusion','kind':'paragraph','fact_indices':[0]},
            {'role':'mechanism','kind':'paragraph','fact_indices':[1]}]}}
    validate_writer_plan(json.dumps(value),selected)
    value['conclusion'][0]['evidence'].append({'ref':1,'unit':3})
    with pytest.raises(ModelOutputError,match='composition_reference_invalid'):validate_writer_plan(json.dumps(value),selected)
    value['conclusion'][0]['evidence']=[{'ref':1,'unit':2}]
    with pytest.raises(ModelOutputError,match='composition_reference_invalid'):validate_writer_plan(json.dumps(value),selected)


def test_multi_case_opening_cannot_select_only_one_available_case():
    source=refine_pack(pack('《甲书》介绍一个案例。《乙书》介绍另一个案例。'))
    question='请结合《甲书》和《乙书》说明为何结果不同。'
    value=plan('conclusion');value['blocks'][0]['groups']=value['blocks'][0]['groups'][:1]
    with pytest.raises(ModelOutputError,match='composition_coverage_invalid'):validate_plan(value,source,question)
    value['blocks'][0]['groups'].append({'label':'第二案例','units':[{'ref':1,'unit':2}]})
    validate_plan(value,source,question)


def test_display_dedup_only_recognizes_layout_not_lost_words_or_numeric_gaps():
    assert context_covered('职 教教师 72.3% 存在 障碍','职教教师72.3%存在障碍')
    assert not context_covered('not here','nothere')
    assert not context_covered('1 000元','1000元')
    assert not context_covered('仅限未拆封商品','商品可以退货')


def test_process_comparison_can_keep_trial_context_and_rounds_without_relaxing_other_plans():
    source=refine_pack(pack('。'.join('阶段材料'+str(i) for i in range(1,21))+'。'))
    value=plan()
    value['blocks'][0]['groups'][0]['units']=[{'ref':1,'unit':i} for i in range(1,7)]
    value['blocks'][0]['groups'][1]['units']=[{'ref':1,'unit':i} for i in range(7,10)]
    for group in value['blocks'][0]['groups']:
        group['label_source']=group['units'][0].copy()
        group['label']='阶段材料'+str(group['label_source']['unit'])
    validate_plan(value,source,'请比较两阶段实践任务。')
    with pytest.raises(ModelOutputError):validate_plan(value,source,'请比较两类概念。')
    over=copy.deepcopy(value)
    for a,b in [(10,16),(16,20)]:
        over['blocks'].append({'role':'comparison','focus':'另一操作维度','part_ids':['p1'],
            'groups':[{'label':'甲','units':[{'ref':1,'unit':i} for i in range(a,b)]},
                      {'label':'乙','units':[{'ref':1,'unit':1}]}]})
    with pytest.raises(ModelOutputError):validate_plan(over,source,'请比较两阶段实践任务。')


def bound_plan():
    return {'blocks':[{'role':'comparison','focus':'操作如何递进','part_ids':['p1'],'groups':[
        {'label':'手工调整','label_source':{'ref':1,'unit':1},'units':[{'ref':1,'unit':1}]},
        {'label':'三轮模型训练','label_source':{'ref':1,'unit':2},
         'units':[{'ref':1,'unit':i} for i in range(2,7)]}]}],'missing_part_ids':[]}


def process_pack():
    return refine_pack(pack('学生手工调整参数。学生完成三轮模型训练，控制样本数量。第一轮投入2张。第二轮投入7张。第三轮投入20张。记录每轮准确率变化。另讨论伦理责任。'))


@pytest.mark.parametrize('label,anchor',[('第一阶段：手工调整',{'ref':1,'unit':2}),
                                       ('手工调整',{'ref':1,'unit':2}),
                                       ('手工调整',{'ref':1,'unit':7})])
def test_process_group_label_must_be_grounded_in_its_own_selected_source(label,anchor):
    value=bound_plan();value['blocks'][0]['groups'][0].update(label=label,label_source=anchor)
    with pytest.raises(ModelOutputError,match='composition_binding_invalid'):
        validate_plan(value,process_pack(),'比较阶段实践任务。')


def test_process_inputs_and_observation_form_a_dependency_without_unasked_background():
    source=process_pack();value=bound_plan()
    assert [u['unit'] for u in process_dependencies(source)[0]['units']]==[2,3,4,5,6]
    validate_plan(value,source,'比较阶段实践任务。')
    value['blocks'][0]['groups'][1]['units'].pop(2)
    with pytest.raises(ModelOutputError,match='composition_process_coverage_invalid'):
        validate_plan(value,source,'比较阶段实践任务。')


def test_bound_comparison_requires_joint_anchors_and_all_process_support_in_output():
    source=process_pack();value=bound_plan()
    fact={'evidence':[{'ref':1,'unit':i} for i in range(1,7)],'part_ids':['p1']}
    body={'conclusion':[fact],'explanation':[],
          'presentation':{'blocks':[{'role':'comparison','kind':'paragraph','fact_indices':[0]}]}}
    validate_writer_plan(json.dumps(body),value,source)
    fact['evidence'].pop(3)
    with pytest.raises(ModelOutputError,match='composition_process_coverage_invalid'):
        validate_writer_plan(json.dumps(body),value,source)
    body['conclusion']=[{'evidence':[{'ref':1,'unit':1}],'part_ids':['p1']},
                        {'evidence':[{'ref':1,'unit':i} for i in range(2,7)],'part_ids':['p1']}]
    body['presentation']['blocks'][0]['fact_indices']=[0,1]
    with pytest.raises(ModelOutputError,match='composition_comparison_unpaired'):
        validate_writer_plan(json.dumps(body),value,source)


def test_shared_summary_cannot_impersonate_both_process_objects():
    value=bound_plan()
    for group in value['blocks'][0]['groups']:
        group['units'].append({'ref':1,'unit':7})
        group['label_source']={'ref':1,'unit':7}
    body={'conclusion':[{'evidence':[{'ref':1,'unit':7}],'part_ids':['p1']}],'explanation':[],
          'presentation':{'blocks':[{'role':'comparison','kind':'paragraph','fact_indices':[0]}]}}
    with pytest.raises(ModelOutputError,match='composition_comparison_unpaired'):
        validate_writer_plan(json.dumps(body),value)


def test_process_plan_uses_pairwise_comparisons_and_no_duplicate_final_summary():
    source=process_pack();value=bound_plan()
    value['blocks'][0]['groups'].append(copy.deepcopy(value['blocks'][0]['groups'][0]))
    with pytest.raises(ModelOutputError,match='composition_plan_invalid'):
        validate_plan(value,source,'比较阶段实践任务。')
    value=bound_plan();value['blocks'].append({**copy.deepcopy(value['blocks'][0]),'role':'conclusion'})
    with pytest.raises(ModelOutputError,match='composition_plan_invalid'):
        validate_plan(value,source,'比较阶段实践任务。')


def test_conceptual_definition_expands_the_same_grounded_concepts_as_opening():
    source=refine_pack(pack('甲概念按用途划分。乙概念按结构划分。另有实践领域的归纳框架。'))
    value=plan('conclusion');value['blocks'].append({**copy.deepcopy(value['blocks'][0]),'role':'definition'})
    validate_plan(value,source,'解释两组概念之间的关系。')
    value['blocks'][1]['groups']=[{'label':'另一框架','units':[{'ref':1,'unit':3}]}]
    with pytest.raises(ModelOutputError,match='composition_binding_invalid'):
        validate_plan(value,source,'解释两组概念之间的关系。')
