"""自然正文编排的协议、发布、恢复与引用负例；全部使用合成证据。"""
import copy
import json
import pytest
from app.rag_validation import ValidatedFact,ValidatedOutput,ModelOutputError,render_markdown
from app.rag_presentation import validate_presentation,render_answer
from app.rag_answer_contract import validate_task_output
from tests.test_rag_numeric_roles import task
from tests.test_question_jobs import question_fixture,submit,LocalRuntime
from app.question_worker import QuestionWorker


def layout(indices=(0,1),**extra):
    return {'blocks':[dict(role='mechanism',kind='paragraph',fact_indices=list(indices),**extra)]}


def output():
    return ValidatedOutput(status='answered',conclusion=[ValidatedFact('甲方案保留本地资料。',[1],claim_type='direct')],
        explanation=[ValidatedFact('因此，适用条件应结合授权范围判断。',[2],claim_type='inference')],presentation=layout())


def test_one_paragraph_preserves_each_sentence_citation_and_inference():
    text=render_answer(output())
    assert text=='甲方案保留本地资料。[1] 【结构推断】因此，适用条件应结合授权范围判断。[2]'
    assert render_markdown(output())==text
    assert '\n\n' not in text


@pytest.mark.parametrize('value',[
    None,{}, {'blocks':'x'}, {'blocks':[]},layout((0,)),layout((0,0)),layout((0,2)),layout((True,1)),
    layout((0,1),text='因此可以免除授权。'),{'blocks':[dict(role='invented',kind='paragraph',fact_indices=[0,1])]},
    {'blocks':[dict(role='mechanism',kind='html',fact_indices=[0,1])]}, {'blocks':[dict(role='mechanism',kind='paragraph',fact_indices=[-1,0])]},
])
def test_invalid_omitted_duplicate_or_free_text_never_published(value):
    with pytest.raises(ModelOutputError):validate_presentation(value,output().conclusion+output().explanation)


def test_reordering_is_presentation_only_not_new_text():
    o=output();o.presentation=layout((1,0));text=render_answer(o)
    assert text.index('[2]')<text.index('[1]')
    assert [f.text for f in o.conclusion+o.explanation]==['甲方案保留本地资料。','因此，适用条件应结合授权范围判断。']


def test_conditions_not_dropped_for_readability():
    o=output();o.conclusion[0].context=[dict(kind='condition',text='取得单位授权')]
    assert '限定：取得单位授权' in render_answer(o)
    o.conclusion[0].text='取得单位授权后，甲方案才可接入。'
    assert render_answer(o).count('取得单位授权')==1


def test_old_v7_does_not_require_presentation():
    assert task('双方共同承担费用。').status=='answered'
    o=output();o.presentation=None
    assert '## 结论' in render_markdown(o)


def test_empty_evidence_uses_empty_blocks():
    assert validate_presentation({'blocks':[]},[])=={'blocks':[]}
    with pytest.raises(ModelOutputError):validate_presentation(layout((0,)),[])


def test_current_worker_persists_and_reloads_paragraph_structure(tmp_path):
    env=question_fixture(tmp_path)
    job,_=submit(env)
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    row=env.service.get_job(job['id']);assert row['status']=='succeeded',row
    answer=env.service.get_result(row['result_id'])['answer']
    assert answer['presentation']['blocks'][0]['fact_indices']==[0]
    assert answer['answer'].endswith('[1]') and not answer['answer'].startswith('##')
    assert answer['conclusion'][0]['evidence_ids']==[answer['citations'][0]['evidence_id']]
    assert env.service.get_result(row['result_id'])['answer']==answer
    exported,_,_=env.service.export(row['result_id'],'markdown')
    assert exported.count(answer['answer'])==1
    assert '## 回答' in exported and '## 依据与说明' not in exported


def test_worker_never_publishes_invalid_layout_even_when_facts_are_valid(tmp_path,caplog):
    from tests.test_question_jobs import REPLY
    raw=json.loads(REPLY);raw['presentation']['blocks'][0]['fact_indices']=[0,0]
    env=question_fixture(tmp_path)
    env.model.handler=lambda *a:json.dumps(raw,ensure_ascii=False)
    job,_=submit(env)
    QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    row=env.service.get_job(job['id'])
    assert row['status']=='failed' and row['result_id'] is None
    assert row['error_code']=='rag_output_invalid'
    assert 'presentation_coverage_mismatch' in caplog.text


def test_current_protocol_rejects_old_response_instead_of_silent_fallback(tmp_path):
    env=question_fixture(tmp_path)
    from tests.test_question_jobs import REPLY
    raw=json.loads(REPLY);raw.pop('presentation')
    # 不通过测试provider自动升级负例，直接走生产协议校验。
    from app.rag_context import EvidenceItem,EvidencePack
    pack=EvidencePack(items=[],candidates=[],index_id='i',parse_version_id='v',truncated=False,context_chars=0)
    with pytest.raises(ModelOutputError,match='presentation_missing'):
        validate_task_output(json.dumps(raw),pack,question='规则',contract_version='fact-context-parts-v8')


def test_stream_groups_known_facts_without_publishing_arbitrary_plan_text():
    from app.answer_stream import FactStreamParser
    groups=[]
    parser=FactStreamParser(lambda c,n,f:groups.append(parser.presentation_block(c,n)))
    raw=dict(presentation=layout(),conclusion=[dict(text='甲')],explanation=[dict(text='乙')])
    for char in json.dumps(raw,ensure_ascii=False):parser.feed(char)
    assert groups==[dict(index=0,kind='paragraph'),dict(index=0,kind='paragraph')]
    with pytest.raises(ModelOutputError):
        FactStreamParser(lambda *a:None).feed(json.dumps(dict(presentation=layout(text='因此不需要审批'))))


def test_causal_question_paragraph_budget_is_enforced_after_real_fact_validation():
    from app.rag_context import EvidenceItem,EvidencePack
    from app.rag_extract_context import assign_unit_options
    texts=['参与者报告工具更方便。','试点未设置随机分组或对照组。']
    items=[EvidenceItem(reference_id=n,document_id='d',parse_version_id='v',chunk_id=str(n),page=1,text=t,
        read_ranges=[[0,len(t)]],segment_policy='sentence-segments-v2',chunk_type='text') for n,t in enumerate(texts,1)]
    assign_unit_options(items,[])
    evidence=EvidencePack(items=items,candidates=[],index_id='i',parse_version_id='v',truncated=False,context_chars=1000)
    facts=[dict(text=t,context=[],claim_type='direct',evidence=[dict(ref=n,unit=1)],part_ids=['p1']) for n,t in enumerate(texts,1)]
    body=dict(status='answered',conclusion=facts,explanation=[],clarification_questions=[],missing_points=[],
        answer_parts=[dict(part_id='p1',state='answered',missing_detail='')],presentation={'blocks':[
            dict(role='evidence',kind='paragraph',fact_indices=[0]),dict(role='evidence',kind='paragraph',fact_indices=[1])]})
    validate_task_output(json.dumps(body),evidence,question='描述试点记录。',contract_version='fact-context-parts-v8')
    with pytest.raises(ModelOutputError,match='presentation_invalid'):
        validate_task_output(json.dumps(body),evidence,question='参与者反馈更方便，是否已经证明提高能力？',contract_version='fact-context-parts-v8')
    body['presentation']=layout()
    validate_task_output(json.dumps(body),evidence,question='参与者反馈更方便，是否已经证明提高能力？',contract_version='fact-context-parts-v8')
