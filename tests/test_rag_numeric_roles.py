"""定位语法与真实数值的成组回归，走生产v7校验；只用合成证据。"""
import json
import pytest

from app.rag_answer_contract import validate_task_output
from app.rag_context import EvidenceItem,EvidencePack
from app.rag_extract_context import assign_unit_options
from app.rag_numeric_roles import numeric_view,locate,number_tokens,source_number_tokens
from app.rag_validation import ModelOutputError
from app.schemas import SourceLocation
from tests.fixtures.rag_quality.claim_protocol import upgrade


def task(text,source='双方共同承担费用。',*,pages=(3,),heading='2 控制 / 2.7 机制',locations=None,context=False):
    items=[EvidenceItem(reference_id=n,chunk_id=f'c{n}',document_id='d',parse_version_id='v',page=page,
                        text=source,heading_path=heading,sources=locations or [SourceLocation(format='pdf',page=page)],
                        segment_policy='sentence-segments-v2') for n,page in enumerate(pages,1)]
    assign_unit_options(items,[])
    pack=EvidencePack(items=items,candidates=items,index_id='i',parse_version_id='v',truncated=False,context_chars=1000)
    fact=dict(text=text,refs=list(range(1,len(items)+1)),context=[dict(kind='condition',ref=1,text=source)] if context else [])
    body=dict(status='answered',conclusion=[fact],explanation=[],clarification_questions=[],missing_points=[],
              evidence_quotes=[dict(ref=n,quote=source) for n in range(1,len(items)+1)],
              answer_parts=[dict(part_id='p1',state='answered',fact_indices=[0],missing_detail='')])
    body=upgrade(body,{n:source for n in range(1,len(items)+1)},strict=True)
    return validate_task_output(json.dumps(body,ensure_ascii=False),pack,question='请说明规则。',contract_version='fact-context-parts-v7')


@pytest.mark.parametrize('locator,pages',[
    ('第3页',(3,)),('第 ３ 页',(3,)),('第三页',(3,)),('第〇三页',(3,)),
    ('第3—5页',(3,4,5)),('第3页至第5页',(3,4,5)),('第3-第5页',(3,4,5)),
    ('第3、5页',(3,5)),('第3页和第5页',(3,5)),('第三至第五页',(3,4,5)),
    ('页码：3—5',(3,4,5)),('页码3',(3,)),('page 3',(3,)),('pp. 3–5',(3,4,5)),
    ('p.3',(3,)),('第3頁',(3,)),('第3页及第5页',(3,5)),
    ('参见3页',(3,)),('详见3—5页',(3,4,5)),('见于第三页',(3,)),
])
def test_page_forms_share_one_grounded_role(locator,pages):
    text=f'规则适用（{locator}）。';result=task(text,pages=pages)
    assert result.conclusion[0].text==text
    assert all(q=='双方共同承担费用。' for q in result.quotes.values())


@pytest.mark.parametrize('locator',[
    '第2章','第二章','第2.7节','2.7节','Section 2.7','Sec. 2.7','章节2.7',
    '第2节2.7','第二章第七节','第2章第2.7节',
])
def test_heading_addresses_do_not_need_measurement_context(locator):
    assert task(f'{locator}说明双方承担费用。').status=='answered'


@pytest.mark.parametrize('locator,source',[
    ('推论6','推论6 共同承担费用。'),('推论六','推论6 共同承担费用。'),
    ('推论（6）','推论(6) 共同承担费用。'),('Corollary 6','推论6 共同承担费用。'),
    ('命题六','命题6 共同承担费用。'),('Theorem 6','定理6 共同承担费用。'),
    ('第二项','1)约定主体；2)共同承担费用。'),('第（二）项','（一）约定主体；（二）共同承担费用。'),
    ('第2条','第2条 共同承担费用。'),('第二款','第二款 共同承担费用。'),
    ('图6','图6 共同承担费用。'),('Figure 6','图6 共同承担费用。'),
    ('表6','表6 共同承担费用。'),('Table 6','表编号：6；共同承担费用。'),
    ('式（12）','公式(12) 共同承担费用。'),('Eq. (12)','式(12) 共同承担费用。'),
    ('表2-1','表2-1 共同承担费用。'),('图2.1','图2.1 共同承担费用。'),
    ('式（2-1）','式(2-1) 共同承担费用。'),
    ('图2、3','图2 共同承担费用；图3 共同承担费用。'),
    ('表2、3','表2 共同承担费用；表3 共同承担费用。'),
    ('推论6第1项','推论61)双方共同承担费用。'),
])
def test_statement_ordinal_figure_table_equation_roles(locator,source):
    assert task(f'{locator}说明共同承担费用。',source).status=='answered'


@pytest.mark.parametrize('locator,pages',[
    ('第3—5页',(3,5)),('第5至3页',(3,4,5)),('第0页',(3,)),
    ('第3至999页',(3,4,5)),('第九十九页',(3,)),('第９９页',(3,)),
])
def test_page_mismatch_gets_locator_error_not_measurement_error(locator,pages):
    with pytest.raises(ModelOutputError,match='page_locator_not_supported'):
        task(f'{locator}说明费用。',pages=pages)


@pytest.mark.parametrize('locator',[
    '第3章','2.8节','第二章第一节','第3节2.7','图2','表2','公式（2）','第2段',
])
def test_unknown_addresses_are_not_silently_ignored(locator):
    with pytest.raises(ModelOutputError,match='locator_not_supported'):
        task(f'{locator}说明费用。')


@pytest.mark.parametrize('value',[
    '3元','3万元','3%','3人','3天','2026年','3kWh','0元','−3元','3.14元','1-2元','3亿元','３元',
])
def test_supported_page_never_exempts_real_data_in_same_sentence(value):
    with pytest.raises(ModelOutputError,match='numeric_context_missing'):
        task(f'第3页规定费用或测量为{value}。',f'费用或测量为{value}。')


@pytest.mark.parametrize('text',['推论6万元的费用。','推论6 万元的费用。','第3批为3元。','第3季度为3元。',
                              '推论6℃时成立。','式12kWh的能耗。','图6公斤的重量。'])
def test_financial_and_process_numbers_are_not_addresses(text):
    with pytest.raises(ModelOutputError,match='numeric_context_missing'):
        task(text,'推论6 约定费用。')


def test_number_from_source_locator_cannot_back_a_business_amount():
    with pytest.raises(ModelOutputError,match='number_not_in_quote'):
        task('费用为6元。','推论6 双方承担费用。',context=True)


def test_real_amount_alongside_source_locator_is_preserved_and_exact_quote_unchanged():
    source='图3约定费用为3元。';text='费用为３元（第３页，图3）。'
    result=task(text,source,context=True)
    assert result.conclusion[0].text==text and result.quotes[1]==source
    assert source_number_tokens(source)=={'3'}


@pytest.mark.parametrize('locator,location',[
    ('第10至12行',SourceLocation(format='txt',line_start=10,line_end=12)),
    ('第2行',SourceLocation(format='docx',table_no=6,row_index=2,col_index=1)),
    ('第1列',SourceLocation(format='docx',table_no=6,row_index=2,col_index=1)),
    ('表6',SourceLocation(format='docx',table_no=6,row_index=2,col_index=1)),
])
def test_structured_source_coordinates_back_line_row_column_and_table(locator,location):
    assert task(f'{locator}说明费用。',pages=(None,),locations=[location]).status=='answered'


def test_locator_role_keeps_offsets_and_ordinary_numbers_separate():
    text='第３页的推论六规定3元。'
    roles=locate(text)
    assert [text[r.start:r.end].strip() for r in roles]==['第３页','推论六']
    assert [r.kind for r in roles]==['page','推论']
    assert number_tokens('第３页金额为3元。')=={'3'}


@pytest.mark.parametrize('prefix',['第一项说明','第三项说明','第一点，','1. ','2) ','（三）说明'])
def test_answer_enumeration_is_not_document_reference(prefix):
    # 中文括号(三)本身不含阿拉伯数字，也不声称文档第几项。
    assert task(prefix+'双方承担费用。').status=='answered'


@pytest.mark.parametrize('prefix',['原文第三项','文档第3项','第3项规定'])
def test_explicit_document_item_still_requires_source(prefix):
    with pytest.raises(ModelOutputError,match='statement_locator_not_supported'):
        task(prefix+'双方承担费用。')


@pytest.mark.parametrize('prefix',['第一项说明','1. '])
def test_answer_order_cannot_hide_business_data(prefix):
    with pytest.raises(ModelOutputError,match='numeric_context_missing'):
        task(prefix+'费用为3元。','费用为3元。')
