"""命题等定位地址必须由本条证据支持；合成原文，不包含业务论文。"""
import json

import pytest

from app.rag_answer_contract import validate_task_output
from app.rag_context import EvidenceItem, EvidencePack
from app.rag_extract_context import assign_unit_options
from app.rag_validation import ModelOutputError
from app.schemas import SourceLocation
from tests.fixtures.rag_quality.claim_protocol import upgrade


def validate(text, source, *, heading='2.7 机制设置', other=None):
    texts=[source]+([other] if other else [])
    items=[EvidenceItem(reference_id=i,chunk_id=f'c{i}',document_id='d',parse_version_id='v',
                        page=4,text=value,heading_path=heading,
                        sources=[SourceLocation(format='pdf',page=4)],segment_policy='sentence-segments-v2')
           for i,value in enumerate(texts,1)]
    assign_unit_options(items,[])
    pack=EvidencePack(items=items,candidates=items,index_id='i',parse_version_id='v',truncated=False,context_chars=1000)
    body=dict(status='answered',conclusion=[dict(text=text,refs=[1],context=[])],explanation=[],
              clarification_questions=[],missing_points=[],evidence_quotes=[dict(ref=1,quote=source)],
              answer_parts=[dict(part_id='p1',state='answered',fact_indices=[0],missing_detail='')])
    body=upgrade(body,{1:source},strict=True)
    return validate_task_output(json.dumps(body,ensure_ascii=False),pack,question='请解释机制？',contract_version='fact-context-parts-v7')


@pytest.mark.parametrize('label',['命题','推论','定理','引理'])
def test_own_statement_number_is_a_locator(label):
    text=f'{label}6说明双方共同承担宣传成本（第4页2.7节）。'
    assert validate(text,f'{label}6 双方共同承担宣传成本。').status=='answered'


def test_ocr_joined_statement_and_enumerated_item_keep_original_text():
    source='推论61)双方回收努力一致；2)宣传关系为kAA<kBB。'
    text='推论6的第2)项给出kAA<kBB（第4页）。'
    result=validate(text,source)
    assert result.conclusion[0].text==text and result.quotes[1]==source


@pytest.mark.parametrize('text,source',[
    ('推论9说明共同承担成本。','推论6 共同承担成本。'),
    ('推论6的第3项给出宣传关系。','推论6 1)共同承担；2)宣传关系。'),
    ('推论61说明共同承担成本。','推论61)共同承担成本。'),
])
def test_unbacked_or_ambiguous_locator_fails(text,source):
    with pytest.raises(ModelOutputError,match='statement_locator_not_supported'):
        validate(text,source)


def test_statement_cannot_borrow_an_uncited_reference():
    with pytest.raises(ModelOutputError,match='statement_locator_not_supported'):
        validate('推论6说明双方承担成本。','双方承担成本。',other='推论6 双方承担成本。')


def test_section_number_is_not_exempt_when_heading_does_not_contain_it():
    with pytest.raises(ModelOutputError,match='locator_not_supported'):
        validate('2.8节说明双方承担成本。','双方承担成本。')


@pytest.mark.parametrize('text',[
    '推论6规定成本为300元。',
    '推论6的第2项规定300元。',
    '2.7节规定成本为300元。',
    '推论6万元的成本。',
])
def test_locator_removal_never_exempts_real_measurements(text):
    with pytest.raises(ModelOutputError,match='numeric_context_missing'):
        validate(text,'推论6 1)主体范围；2)成本为300元。')
