"""复放只使用已公布小段；相邻范围可合并，隐藏尾句不可随之进入。"""
import json

import pytest

from scripts.evaluate_answer_obligations import restore_writer_pack
from app.rag_extract_context import visible_segments
from app.rag_answer_contract import ANSWER_CONTRACT_VERSION,validate_task_output
from app.rag_validation import ModelOutputError


def inputs():
    first='甲条已登记。';second='乙条未登记。';hidden='丙条不可见。'
    case={'id':'independent','question':'说明条款。','sources':[{'id':'s','text':first+second+hidden}]}
    reference={'ref':1,'document_name':'s.txt','content':[{'span':1,'segments':{'1':first,'2':second}}],
        'quote_units':{'1':[[1,1,1]],'2':[[1,2,2]],'3':[[1,3,3]]}}
    observed={'question':case['question'],'model_inputs':[['synthetic',json.dumps({'question':case['question'],'references':[reference]})]]}
    return case,observed,first+second


def test_adjacent_visible_segments_merge_without_admitting_a_hidden_tail():
    case,observed,visible=inputs();pack,_=restore_writer_pack(case,observed)
    assert pack.items[0].read_ranges==[[0,len(visible)]]
    offered=visible_segments(pack.items[0].text,pack.items[0].read_ranges,strict=True)
    assert [(number,''.join(segments.values())) for number,segments,_ in offered]==[(1,visible)]
    assert all('丙条' not in ''.join(segments.values()) for _,segments,_ in offered)
    body={'status':'answered','conclusion':[{'text':'丙条不可见。','part_ids':['p1'],'claim_type':'direct','context':[],
        'evidence':[{'ref':1,'unit':3}]}],'explanation':[],'clarification_questions':[],'missing_points':[],
        'answer_parts':[{'part_id':'p1','state':'answered','missing_detail':''}],
        'presentation':{'blocks':[{'role':'evidence','kind':'paragraph','fact_indices':[0]}]}}
    with pytest.raises(ModelOutputError,match='quote_not_in_admitted_input'):
        validate_task_output(json.dumps(body),pack,question='说明条款。',contract_version=ANSWER_CONTRACT_VERSION)


def test_changed_visible_text_cannot_be_silently_rebuilt_from_the_original():
    case,observed,_=inputs();request=json.loads(observed['model_inputs'][-1][1])
    request['references'][0]['content'][0]['segments']['1']='甲条未登记。'
    observed['model_inputs'][-1][1]=json.dumps(request)
    with pytest.raises(ValueError,match='visible_source_changed'):restore_writer_pack(case,observed)


def test_a_valid_response_to_another_question_cannot_enter_the_review():
    case,observed,_=inputs();observed['question']='另一问题。'
    with pytest.raises(ValueError,match='observed_question_identity_changed'):restore_writer_pack(case,observed)
