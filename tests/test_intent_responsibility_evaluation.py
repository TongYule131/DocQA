"""整问评估器的计分与冻结守卫；不运行全仓回归或真实供应商。"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.evaluate_intent_responsibilities import FrozenModel, score, validate_inputs


def test_frozen_stub_must_keep_all_visible_fragments_and_cannot_use_a_hidden_tail():
    catalog=[{'ref':1,'unit':1,'text':'值班人员负责保管；'}, {'ref':1,'unit':2,'text':'停用后须交还。'}]
    assert FrozenModel.select_units(catalog,['值班人员负责保管；停用后须交还。'])==[{'ref':1,'unit':1},{'ref':1,'unit':2}]
    with pytest.raises(ValueError,match='not_fully_visible'):
        FrozenModel.select_units(catalog[:1],['值班人员负责保管；停用后须交还。'])


def test_unapproved_labels_or_implicit_classification_flags_cannot_enter_evaluation():
    fixtures=json.loads((Path(__file__).parent/'fixtures/intents/responsibility_inputs.json').read_text('utf-8'))
    cases=[{'id':i,'split':'development' if i.startswith('IR-D') else 'calibration' if i.startswith('IR-C') else 'holdout',
            'classification_score_eligible':True} for i in fixtures['cases']]
    value={'sealed':False,'review_status':'pending_human_review','cases':cases}
    with pytest.raises(ValueError,match='seal_required'):validate_inputs(value,fixtures)
    value.update(sealed=True,review_status='accepted_by_user');validate_inputs(value,fixtures)
    value['cases'][0]['classification_score_eligible']='false'
    with pytest.raises(ValueError,match='explicit_classification_boolean'):validate_inputs(value,fixtures)


def example():
    case={'id':'independent-safety','split':'development','classification_score_eligible':False,
        'expected':{'action':'deny','route_kind':'none','outcome':'denied','calls':{'embedding':0,'generation':0,'tool_http':0},
            'status_contract':{'entry.http_status':404,'new_business_task':False}}}
    observed={'actual':{'action':'deny','route_kind':'none','outcome':'denied'},'result':None,'payload':None,
        'main_calls':{'embedding':0,'generation':0,'tool_http':0},'expected_document_ids':[],'corpus_texts':{},
        'new_tasks':0,'phase_calls':{},'entry_http_calls':{'setup':4,'case':1,'verification':0},
        'entries':[{'phase':'case','method':'POST','path':'/api/chat/conversations','http_status':404,'response':{'detail':'未获授权'}}]}
    return case,observed


def test_http200_cannot_be_relabeled_denied_and_hidden_call_cannot_be_ignored():
    case,observed=example();assert score(case,observed)['engineering_passed']
    changed=deepcopy(observed);changed['entries'][0]['http_status']=200
    assert score(case,changed)['dimensions']['status_namespaces'] is False
    changed=deepcopy(observed);changed['main_calls']['generation']=1
    result=score(case,changed)
    assert result['dimensions']['safety'] is False and result['dimensions']['calls'] is False
    assert result['dimensions']['classification'] is None
    assert result['entry_http_calls']['case']==1
    assert result['dimensions']['objects_parameters_clarification'] is None
    assert result['dimensions']['entry_http_contract'] is None
    assert result['dimensions']['call_phase_contract'] is None


def test_successful_tool_response_must_name_the_configured_resource_and_actual_arguments():
    case,observed=example()
    case['expected'].update(action='query_tool',route_kind='MCP',outcome='answered',calls={'embedding':0,'generation':1,'tool_http':3})
    case['expected'].pop('status_contract')
    observed.update(actual={'action':'query_tool','route_kind':'MCP','outcome':'answered'},
        payload={'route':{'kind':'MCP','tool_id':'development-readonly-tool'}},
        result={'status':'answered','tool_source':{'id':'unregistered-other-tool'},'tool_arguments':{'station':'甲'}},
        main_calls={'embedding':0,'generation':1,'tool_http':3},business_http=[{'method':'initialize'},
            {'method':'notifications/initialized'},{'method':'tools/call','params':{'arguments':{'station':'甲'}}}])
    assert score(case,observed)['dimensions']['resources_and_sources'] is False
    observed['result']['tool_source']['id']='development-readonly-tool'
    assert score(case,observed)['dimensions']['resources_and_sources'] is True
