"""入选仍按相关性，任务消息按可靠原文顺序；不改原文、不扩大输入。"""
import json

import pytest

from app.config import Settings
from app.quote_spans import task_evidence_payload
from app.rag_context import build_evidence, EvidenceItem, EvidencePack
from app.rag_answer_contract import validate_task_output
from app.rag_validation import ModelOutputError


def hits():
    return [dict(chunk_id=f'c{i}',document_id='d',parse_version_id='v',page=1,
        text=f'阶段{i}的原始说明。',score=score,order_index=i)
        for i,score in [(2,0.9),(0,0.8),(1,0.7)]]


def test_selected_set_budget_and_quotes_unchanged_when_restoring_order():
    search=dict(index_id='i',parse_version_id='v',results=hits())
    settings=Settings(rag_retrieval_k=3,rag_context_k=3)
    old=build_evidence(search,settings,payload_transform=task_evidence_payload).pack
    new=build_evidence(search,settings,payload_transform=task_evidence_payload,preserve_document_order=True).pack
    assert [i.chunk_id for i in old.items]==['c2','c0','c1']
    assert [i.chunk_id for i in new.items]==['c0','c1','c2']
    assert [(i.reference_id,i.text) for i in new.items]==[(1,'阶段0的原始说明。'),(2,'阶段1的原始说明。'),(3,'阶段2的原始说明。')]
    assert {i.chunk_id:i.text for i in old.items}=={i.chunk_id:i.text for i in new.items}
    assert new.context_chars==old.context_chars==len(json.dumps(task_evidence_payload(new.payload()),ensure_ascii=False,separators=(',',':')))


def test_document_order_does_not_rescue_lower_ranked_items_over_selection_limit():
    search=dict(index_id='i',parse_version_id='v',results=hits())
    result=build_evidence(search,Settings(rag_retrieval_k=3,rag_context_k=2),preserve_document_order=True)
    assert [i.chunk_id for i in result.pack.items]==['c0','c2']
    assert 'c1' not in {i.chunk_id for i in result.pack.items}


@pytest.mark.parametrize('orders',[[0,None,1],[0,0,1]])
def test_missing_or_duplicate_source_order_keeps_relevance_order_with_limitation(orders):
    results=hits()
    for hit,order in zip(results,orders): hit['order_index']=order
    result=build_evidence(dict(index_id='i',parse_version_id='v',results=results),
        Settings(rag_retrieval_k=3,rag_context_k=3),preserve_document_order=True)
    assert [i.chunk_id for i in result.pack.items]==['c2','c0','c1']
    assert any('顺序信息不完整' in note for note in result.limitations)


def test_context_cannot_borrow_unselected_window_even_when_original_contains_phrase():
    text='甲'*610+'后续阶段原文条件'
    item=EvidenceItem(reference_id=1,chunk_id='c',document_id='d',parse_version_id='v',page=1,text=text)
    pack=EvidencePack(items=[item],candidates=[item],index_id='i',parse_version_id='v',truncated=False,context_chars=0)
    body=dict(status='answered',evidence_quotes=[dict(ref=1,span=1)],
        conclusion=[dict(context=[dict(kind='condition',text='后续阶段原文条件',ref=1)],text='条件见后续阶段。',refs=[1])],
        explanation=[],clarification_questions=[],missing_points=[],
        answer_parts=[dict(part_id='p1',state='answered',fact_indices=[0],missing_detail='')])
    with pytest.raises(ModelOutputError,match='context_not_in_quote'):
        validate_task_output(json.dumps(body,ensure_ascii=False),pack,question='条件是什么？')
