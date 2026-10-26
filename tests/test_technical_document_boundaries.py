"""协议标识、真实测量及题干分析标签的通用边界，使用合成资料。"""
import json
from http import HTTPStatus
import pytest
from app.rag_context import EvidenceItem,EvidencePack
from app.rag_extract_context import assign_unit_options
from app.rag_answer_contract import validate_task_output,ANSWER_CONTRACT_VERSION
from app.rag_validation import ModelOutputError,validate_user_condition_boundary
from app.rag_numeric_roles import source_number_tokens


def answer(text,source,*,kind='direct',context=False,question='请解释资料。'):
    item=EvidenceItem(1,'c','d','v',None,source,segment_policy='sentence-segments-v2',read_ranges=[[0,len(source)]])
    assign_unit_options([item],[]);pack=EvidencePack([item],[item],'i','v',False,len(source))
    fact={'text':text,'claim_type':kind,'evidence':[{'ref':1,'unit':1}],'part_ids':['p1'],
          'context':[{'kind':'condition','ref':1,'span':1,'segment':1}] if context else []}
    value={'status':'answered','conclusion':[fact],'explanation':[],'clarification_questions':[],'missing_points':[],
           'answer_parts':[{'part_id':'p1','state':'answered','missing_detail':''}],
           'presentation':{'blocks':[{'role':'conclusion','kind':'paragraph','fact_indices':[0]}]}}
    result=validate_task_output(json.dumps(value,ensure_ascii=False),pack,question=question,contract_version=ANSWER_CONTRACT_VERSION)
    validate_user_condition_boundary(result,pack,question=question)
    return result


@pytest.mark.parametrize('status',list(HTTPStatus))
def test_sourced_http_status_lines_are_not_measurements(status):
    text=f'响应使用 `HTTP/1.1 {status.value} {status.phrase}`。'
    result=answer(text,text)
    assert result.conclusion[0].text==text and result.quotes[1]==text


@pytest.mark.parametrize('text',['响应为 HTTP/2。','状态为 503 Service Unavailable。','HTTP状态码：404。','HTTP 200。'])
def test_typed_protocol_forms_are_checked_against_own_source(text):
    source='响应使用 HTTP/2，失败返回 503 Service Unavailable 或 404 Not Found，成功返回 200 OK。'
    assert answer(text,source).conclusion[0].text==text


@pytest.mark.parametrize('text',[
    '响应为 HTTP/1.0 500 Internal Server Error。','响应为 HTTP/1.1 200 OK。',
    '响应为 HTTP/01.0 200 OK。','HTTP状态码：404。',
])
def test_wrong_status_or_version_is_not_exempt(text):
    with pytest.raises(ModelOutputError,match='technical_literal_not_supported'):
        answer(text,'响应为 HTTP/1.0 200 OK。')


@pytest.mark.parametrize('text',[
    '耗时为3秒。','比例为3%。','`listen(100)`控制连接数。','价格为`200 OK`元。',
    '费用为200元。','响应耗时为 HTTP 200 ms。','200 OK后等待5秒。',
])
def test_backticks_and_protocol_context_do_not_hide_measurements(text):
    with pytest.raises(ModelOutputError,match='numeric_context_missing'):
        answer(text,text)


def test_protocol_numbers_cannot_support_a_business_amount():
    with pytest.raises(ModelOutputError,match='number_not_in_quote'):
        answer('费用为200元。','响应为 HTTP/1.0 200 OK。',context=True)
    assert source_number_tokens('HTTP/1.0 200 OK；费用为3元。')=={'3'}


def test_grounded_coordinated_analysis_label_is_not_a_verbatim_source_claim():
    source='检索负责寻找候选；复核负责核对实际来源。'
    text='“检索+复核”分别处理候选发现与来源核对两个环节。'
    result=answer(text,source,kind='inference',question='分析“检索+复核”的作用。')
    assert result.conclusion[0].claim_type=='inference' and result.quotes[1]==source


@pytest.mark.parametrize('kind',['direct','synthesis'])
def test_analysis_label_still_requires_an_inference_declaration(kind):
    with pytest.raises(ModelOutputError,match='mixed_user_source'):
        answer('“检索+复核”处理两个环节。','检索寻找候选，复核核对来源。',kind=kind,question='分析“检索+复核”。')


@pytest.mark.parametrize('source,text,reason',[
    ('检索寻找候选。','“检索+复核”处理两个环节。','mixed_user_source'),
    ('检索寻找候选，复核核对来源。','文档将这种结构命名为“检索+复核”。','mixed_user_source'),
    ('检索与复核各自处理一部分。','“检索+200元”是一种分析关系。','numeric_context_missing'),
    ('NoSQL负责数据持久化，检索负责发现候选。','“SQL+检索”处理两个环节。','mixed_user_source'),
])
def test_missing_components_and_invented_source_names_stay_rejected(source,text,reason):
    question='请分析'+text[text.index('“'):text.index('”')+1]
    with pytest.raises(ModelOutputError,match=reason):answer(text,source,kind='inference',question=question)


def test_markdown_layout_comparison_preserves_actual_quotes():
    source='跨域请求**需要校验授权**后才可执行。'
    text='“跨域请求需要校验授权”给出了执行条件。'
    result=answer(text,source,question='解释“跨域请求需要校验授权”。')
    assert result.conclusion[0].text==text and result.quotes[1]==source
