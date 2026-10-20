"""限定语编号选择回取真实原文；旧逐字协议不做OCR清洗或跨窗口借用。"""
import json
from copy import deepcopy

import pytest

from app.quote_spans import context_segments, task_segment_payload, quote_choices
from app.rag_answer_contract import validate_task_output
from app.rag_context import EvidenceItem, EvidencePack
from app.rag_validation import ModelOutputError
from app.rag_prompts import answer_requirements, build_task_user_prompt


def fixture(text='项目需要100张图片。驱 动性项目只作训练，不是测试结果。'):
    item=EvidenceItem(reference_id=1,chunk_id='c',document_id='d',parse_version_id='v',page=1,text=text)
    pack=EvidencePack(items=[item],candidates=[item],index_id='i',parse_version_id='v',truncated=False,context_chars=0)
    body=dict(status='answered',evidence_quotes=[dict(ref=1,span=1)],conclusion=[dict(text='训练使用100张图片。',refs=[1],
        context=[dict(kind='condition',ref=1,segment=1)])],explanation=[],clarification_questions=[],missing_points=[],
        answer_parts=[dict(part_id='p1',state='answered',fact_indices=[0],missing_detail='')])
    return pack,body


@pytest.mark.parametrize('text',['', '汉 字\r\n原文，条件不变。'*30, '甲'*1300, '原文[18]保留 21% 与 0.0，禁止推算。'*40])
def test_segment_payload_keeps_every_quote_character_and_bounds(text):
    spans=context_segments(text)
    assert ''.join(spans)==text.replace('\r\n','\n')
    assert all(0<len(s)<=60 for s in spans)
    payload=task_segment_payload([dict(ref=1,content=text)])
    assert [''.join(c['segments'].values()) for c in payload[0]['content']]==quote_choices(text)
    for choice in payload[0]['content']:
        assert list(choice['segments']) == [str(n) for n in range(1,len(choice['segments'])+1)]
        assert list(choice['segments'].values()) == context_segments(''.join(choice['segments'].values()))


def test_current_selector_returns_literal_ocr_text_without_free_copy_or_repair():
    pack,body=fixture()
    out=validate_task_output(json.dumps(body,ensure_ascii=False),pack,question='训练使用多少图片？')
    assert out.conclusion[0].context==[dict(kind='condition',ref=1,text=pack.items[0].text)]
    assert out.context_selection_count==1
    assert '驱 动性项目' in out.conclusion[0].context[0]['text']
    body['conclusion'][0]['context']=[dict(kind='condition',ref=1,text='驱动性项目')]
    with pytest.raises(ModelOutputError,match='context_not_in_quote'):
        validate_task_output(json.dumps(body,ensure_ascii=False),pack,question='训练使用多少图片？')


@pytest.mark.parametrize('segment',[True,0,-1,99,'1',1.0])
def test_bad_selector_never_publishes(segment):
    pack,body=fixture();body['conclusion'][0]['context'][0]['segment']=segment
    with pytest.raises(ModelOutputError):validate_task_output(json.dumps(body),pack,question='数量多少？')


@pytest.mark.parametrize('version',['fact-context-parts-v1','fact-context-parts-v2'])
def test_old_protocol_keeps_strict_text_and_rejects_new_selector(version):
    pack,body=fixture()
    with pytest.raises(ModelOutputError):
        validate_task_output(json.dumps(body),pack,question='数量多少？',contract_version=version)
    body['conclusion'][0]['context']=[dict(kind='condition',ref=1,text='驱动性项目')]
    with pytest.raises(ModelOutputError,match='context_not_in_quote'):
        validate_task_output(json.dumps(body),pack,question='数量多少？',contract_version=version)


def test_selector_is_relative_to_chosen_window_not_another_window():
    pack,body=fixture('甲'*610+'后续条件100张。')
    body['conclusion'][0]['text']='原文要求100张。'
    # 首窗口只有600个甲；后续窗口有数值，不能借来支持首窗口事实。
    with pytest.raises(ModelOutputError,match='number_not_in_quote'):
        validate_task_output(json.dumps(body),pack,question='数量多少？')


def test_canonical_saved_context_revalidates_in_current_contract():
    pack,body=fixture()
    out=validate_task_output(json.dumps(body),pack,question='数量多少？')
    saved=deepcopy(body);saved['conclusion'][0]['context']=out.conclusion[0].context
    again=validate_task_output(json.dumps(saved),pack,question='数量多少？')
    assert again.conclusion==out.conclusion


def test_requirement_checklist_comes_from_question_and_preserves_source_and_part_ids():
    question='监测系统有哪些层次和关键模块？各模块如何对应故障处理？'
    original='来源仅记录一个部署日期，未说明架构。'
    body=json.loads(build_task_user_prompt(question=question,document_name='设备',evidence_payload=[dict(ref=1,content=original)]))
    assert body['question']==question
    assert [p['part_id'] for p in body['answer_requirements']]==[p['part_id'] for p in body['question_parts']]
    assert any('逐个' in check for check in body['answer_requirements'][0]['checks'])
    assert ''.join(body['references'][0]['content'][0]['segments'].values())==original
    assert original not in json.dumps(body['answer_requirements'],ensure_ascii=False)
    assert answer_requirements('退费窗口何时开放？')[0]['checks']==[]
    assert any('具体值' in c for c in answer_requirements('商业租金分别是多少？')[0]['checks'])
    assert any('逐轮' in c and '单位' in c for c in answer_requirements('各课时主要实践任务是什么？')[0]['checks'])
