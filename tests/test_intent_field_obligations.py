"""字段证据不能靠默认值、错字段或缺前置得到通过；不判断生成语义。"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.intent_obligations import field_obligations,tool_input_contract,joint_claim_uses_sources
from scripts.evaluate_intent_responsibilities import command_exit_code,select_cases


@pytest.mark.parametrize('arguments,command,expected',[
    ({'department':'甲科'},'执行登记查询：部门=甲科。',True),
    ({'department':None},'执行登记查询：部门=甲科。',False),
    ({'department':'乙科'},'执行登记查询：部门=甲科。',False),
    ({'department':'甲科'},'执行登记查询：楼层=甲科。',False),
    ({'department':None},'执行登记查询。',True),
])
def test_parameter_field_and_missing_value_must_match_the_actual_original_input(arguments,command,expected):
    tool={'properties':{'department':{'title':'部门'}},'command':command,'initial_arguments':arguments}
    assert tool_input_contract(tool)[0] is expected


def test_frozen_tool_inputs_do_not_synthesize_missing_fields_from_explicit_values():
    inputs=json.loads((Path(__file__).parent/'fixtures/intents/responsibility_inputs.json').read_text('utf-8'))
    failures={key:tool_input_contract(row['tool'])[1] for key,row in inputs['cases'].items()
              if row.get('tool') and not tool_input_contract(row['tool'])[0]}
    assert not failures,failures


def test_scope_narrowing_fixture_contains_a_real_other_document():
    inputs=json.loads((Path(__file__).parent/'fixtures/intents/responsibility_inputs.json').read_text('utf-8'))
    selected=inputs['cases']['IR-H06']['selected_documents']
    docs=inputs['corpora']['holdout']
    assert any(d['key']==selected for d in docs)
    assert any(d['key']!=selected and any('检修前' in p for p in d['paragraphs']) for d in docs)


@pytest.mark.parametrize('claim_type',['synthesis','inference'])
def test_joint_source_audit_accepts_both_legal_types_but_keeps_each_document_required(claim_type):
    assert joint_claim_uses_sources({'claim_type':claim_type,'refs':[1,2,3]},{1:'a',2:'b',3:'b'},['a','b'])
    assert not joint_claim_uses_sources({'claim_type':claim_type,'refs':[1,3]},{1:'a',2:'b',3:'a'},['a','b'])
    assert not joint_claim_uses_sources({'claim_type':'direct','refs':[1,2]},{1:'a',2:'b'},['a','b'])
    assert not joint_claim_uses_sources({'claim_type':claim_type,'refs':[]},{},[])


def test_preservation_and_waiting_replay_without_evidence_remain_unmeasured():
    expected={'action':'stop','objects_parameters_clarification':'保留旧成功版本','resources':'旧原件和索引保持'}
    case={'id':'IR-C12','question':'说明原规则。','expected':expected}
    observation={'entries':[],'payload':None,'result':None}
    result=field_obligations(case,observation,{})
    assert result['passed']==0 and result['unmeasured']==1 and not result['complete']


def test_tool_result_cannot_change_confirmed_values_even_when_ledger_and_route_are_correct():
    fixture={'tool':{'command':'登记查询：部门=甲科。','properties':{'department':{'title':'部门'}},
                     'initial_arguments':{'department':'甲科'}}}
    case={'id':'independent-tool','question':fixture['tool']['command'],
          'expected':{'action':'query_tool','outcome':'answered','objects_parameters_clarification':'部门不变'}}
    observation={'entries':[],'payload':None,'result':{'tool_arguments':{'department':'甲科'}},
        'tool_parameter_checkpoint':{'department':'甲科'},'business_http':[{'params':{'arguments':{'department':'甲科'}}}]}
    assert field_obligations(case,observation,fixture)['complete']
    changed=deepcopy(observation);changed['result']['tool_arguments']['department']='乙科'
    assert field_obligations(case,changed,fixture)['failed']==1


def test_subset_selection_rejects_unknown_duplicate_or_cross_split_ids_and_failed_cli_is_nonzero():
    cases=[{'id':'a','split':'development'},{'id':'b','split':'holdout'}]
    assert select_cases(cases,'development',['a'])==cases[:1]
    for ids in ([],['a','a'],['b'],['unknown']):
        with pytest.raises(ValueError,match='case_selection'):select_cases(cases,'development',ids)
    assert command_exit_code({'engineering_passed':0,'total':0})==1
    assert command_exit_code({'engineering_passed':2,'total':3})==1
    assert command_exit_code({'engineering_passed':3,'total':3})==0
