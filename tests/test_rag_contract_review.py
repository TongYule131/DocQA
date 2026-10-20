"""上一版遗漏的生产路径回归；只使用合成原文和本地替身。"""
import copy
import json

import pytest

from app.rag_answer_contract import validate_task_output
from app.rag_validation import ModelOutputError, ValidatedFact, ValidatedOutput, render_markdown
from app.question_worker import QuestionWorker
from app.schemas import Chunk, SourceLocation
from scripts.evaluate_answer_contract import pack_for
from tests.fixtures.rag_quality.answer_contract import make_cases
from tests.test_question_jobs import LocalRuntime, question_fixture, submit
from tests.test_rag_answer_contract import _context_environment
from tests.fixtures.rag_quality.claim_protocol import upgrade


def _markdown(answer):
    return render_markdown(ValidatedOutput(status=answer['status'],
        conclusion=[ValidatedFact(**{k:v for k,v in f.items() if k!='evidence_ids'}) for f in answer['conclusion']],
        explanation=[ValidatedFact(**{k:v for k,v in f.items() if k!='evidence_ids'}) for f in answer['explanation']],
        clarification_questions=answer['clarification_questions'],missing_points=answer['missing_points']))


def _prepared_checkpoint(env, question):
    job,_=submit(env,question=question)
    worker=QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime())
    claimed=worker.store.claim(worker.worker_id,60,job_id=job['id'])
    evidence={}
    def observer(stage, details):
        if stage=='evidence_fixed':evidence.update(details['diagnostics'])
        if stage=='query_completed':
            assert worker.store.save_checkpoint(job['id'],claimed['lease_token'],role='retrieval',payload=details['search_result'],sequence_no=None)
    result=env.rag.answer('qdoc',question,snapshot=json.loads(claimed['snapshot_json']),observer=observer)
    assert worker.store.save_checkpoint(job['id'],claimed['lease_token'],role='evidence',payload=evidence,sequence_no=None)
    return worker,claimed,result.answer.model_dump()


def _resume(env,worker,claimed,answer):
    assert worker.store.save_checkpoint(claimed['id'],claimed['lease_token'],role='answer',payload=answer,sequence_no=None)
    count=(len(env.model.calls),len(env.embedding.calls))
    worker.process_job(claimed)
    assert count==(len(env.model.calls),len(env.embedding.calls))
    return env.service.get_job(claimed['id'])


def test_valid_nonascending_fact_references_publish_with_consistent_canonical_markers(tmp_path):
    env=question_fixture(tmp_path)
    env.repository.finish_parse('qdoc',1,[Chunk(id='r'+str(i),document_id='qdoc',page=1,
        text=text,parse_version_id='refs-v',order_index=i,
        sources=[SourceLocation(format='txt',line_start=i+1,line_end=i+1)])
        for i,text in enumerate(['教学内容涵盖认识神经元。','实践任务安排水果识别。'])])
    env.rag.index.build('qdoc',rebuild=True);env.embedding.calls.clear()
    def answer(_system,user):
        refs=json.loads(user)['references']; assert len(refs)==2
        return json.dumps(upgrade(dict(status='answered',conclusion=[dict(text='材料介绍教学内容和实践任务。',
            refs=[refs[1]['ref'],refs[0]['ref']],context=[])],explanation=[],clarification_questions=[],
            evidence_quotes=[dict(ref=r['ref'],span=1) for r in refs],
            answer_parts=[dict(part_id='p1',state='answered',fact_indices=[0],missing_detail='')]),
            {r['ref']:''.join(r['content'][0]['segments'].values()) for r in refs}),ensure_ascii=False)
    env.model.handler=answer
    job,_=submit(env,question='介绍哪些教学内容和任务？')
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    done=env.service.get_job(job['id']);assert done['status']=='succeeded',done
    result=env.service.get_result(done['result_id'])['answer']
    assert result['conclusion'][0]['refs']==[1,2]
    assert '[1][2]' in result['answer']
    assert len(env.model.calls)==len(env.embedding.calls)==1


def test_one_question_can_preserve_partial_facts_and_a_real_missing_item():
    case=make_cases()[0];body=copy.deepcopy(case['positive'])
    body.update(status='partial',missing_points=['没有提供成本数据。'])
    body['answer_parts']=[dict(part_id='p1',state='partial',fact_indices=[0],missing_detail=body['missing_points'][0])]
    result=validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question='收入和成本分别是多少？')
    assert result.status=='partial' and result.conclusion and result.answer_parts[0]['state']=='partial'


@pytest.mark.parametrize('amount',['-10','+10'])
def test_numeric_presence_check_preserves_signs_after_chinese_characters(amount):
    case=copy.deepcopy(make_cases()[0]);case['source']='甲社2025年收入10万元，未经审计。'
    body=case['positive'];body['conclusion'][0].update(text='甲社2025年收入'+amount+'万元。',
        context=[dict(kind='unit',text='万元',ref=1)])
    with pytest.raises(ModelOutputError,match='number_not_in_quote'):
        validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question=case['question'])


def test_checkpoint_rechecks_user_condition_source_boundary_before_publication(tmp_path):
    env,case=_context_environment(tmp_path)
    phrase='本班另有新增学员'
    worker,claimed,answer=_prepared_checkpoint(env,'我的情况是“'+phrase+'”，'+case['question'])
    answer['conclusion'][0]['text']+='“'+phrase+'”。'
    answer['answer']=_markdown(answer)
    done=_resume(env,worker,claimed,answer)
    assert done['status']=='failed' and not done['result_id']


def test_checkpoint_insufficient_answer_cannot_replace_fixed_fallback(tmp_path):
    env,case=_context_environment(tmp_path)
    body=dict(status='insufficient_evidence',conclusion=[],explanation=[],clarification_questions=[],
        missing_points=[],answer_parts=[dict(part_id='p1',state='missing_evidence',missing_detail='缺少成本依据。')])
    env.model.handler=lambda *_:json.dumps(body,ensure_ascii=False)
    worker,claimed,answer=_prepared_checkpoint(env,case['question'])
    answer['answer']='已经证实该机构利润大幅增长。'
    done=_resume(env,worker,claimed,answer)
    assert done['status']=='failed' and not done['result_id']


def test_insufficient_answer_exports_the_specific_missing_detail(tmp_path):
    env,case=_context_environment(tmp_path)
    detail='本次证据没有2026年的经营数据。'
    body=dict(status='insufficient_evidence',conclusion=[],explanation=[],clarification_questions=[],
        missing_points=[],answer_parts=[dict(part_id='p1',state='missing_evidence',missing_detail=detail)])
    env.model.handler=lambda *_:json.dumps(body,ensure_ascii=False)
    worker,claimed,answer=_prepared_checkpoint(env,case['question'])
    done=_resume(env,worker,claimed,answer)
    assert done['status']=='succeeded'
    assert detail in env.service.export(done['result_id'],'markdown')[0]


def test_clarification_detail_is_also_part_of_total_text_budget():
    case=copy.deepcopy(make_cases()[0])
    text='资料列出了调查方法，但用户尚未指定需要比较的对象。'*12
    case['source']=text
    detail='请明确需要比较的对象与范围。'*35
    body=dict(status='clarification_needed',conclusion=[],
        explanation=[dict(text=text,refs=[1],context=[])],clarification_questions=['比较哪些对象？'],
        evidence_quotes=[dict(ref=1,span=1)],answer_parts=[dict(part_id='p'+str(i),state='needs_clarification',
            fact_indices=[0],missing_detail=detail) for i in range(1,9)])
    assert len(text)+8*len(detail)>4000 and len(detail)<=500
    with pytest.raises(ModelOutputError,match='text_too_long'):
        validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question='；'.join(['比较范围']*8))


@pytest.mark.parametrize('version',['unknown',None,{}])
def test_unknown_checkpoint_contract_versions_are_rejected(version):
    case=make_cases()[0]
    with pytest.raises(ModelOutputError,match='unsupported_contract_version'):
        validate_task_output(json.dumps(case['positive'],ensure_ascii=False),pack_for(case),question=case['question'],contract_version=version)


def test_legacy_v1_does_not_silently_gain_new_partial_part_protocol():
    case=make_cases()[0];body=copy.deepcopy(case['positive'])
    body.update(status='partial',missing_points=['未提供成本依据。'])
    body['answer_parts']=[dict(part_id='p1',state='partial',fact_indices=[0],missing_detail=body['missing_points'][0])]
    with pytest.raises(ModelOutputError,match='coverage_mismatch'):
        validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question='收入与成本？',contract_version='fact-context-parts-v1')


def test_saved_v6_answer_checkpoint_remains_publishable_without_new_calls(tmp_path):
    env,case=_context_environment(tmp_path)
    worker,claimed,answer=_prepared_checkpoint(env,case['question'])
    snapshot=json.loads(claimed['snapshot_json'])
    snapshot.update(answer_contract_version='fact-context-parts-v1',task_prompt_version='rag-task-v6')
    for field in ('evidence_selection_version','evidence_profile','claim_review_version'):snapshot.pop(field,None)
    claimed['snapshot_json']=json.dumps(snapshot,ensure_ascii=False)
    with env.repository.connect() as db:
        db.execute('UPDATE question_jobs SET snapshot_json=? WHERE id=?',(claimed['snapshot_json'],claimed['id']))
    answer['prompt_version']='rag-task-v6'
    # 旧v1一条引用对应整块，不带新单位拆分/稳定ID或认证。
    citation=dict(answer['citations'][0],reference_id=1,quote=case['source'],source_reference_id=None,
        evidence_id=None,char_start=None,char_end=None,quote_sha256=None,offset_basis=None)
    answer['citations']=[citation]
    for fact in answer['conclusion']+answer['explanation']:
        fact.update(refs=[1],context=case['positive']['conclusion'][0]['context'],claim_type='unclassified',evidence_ids=[],support_review={})
    answer['answer']=_markdown(answer)
    done=_resume(env,worker,claimed,answer)
    assert done['status']=='succeeded'
    assert env.service.get_result(done['result_id'])['answer']['prompt_version']=='rag-task-v6'


def test_partial_single_question_survives_worker_history_and_export(tmp_path):
    env,case=_context_environment(tmp_path)
    body=copy.deepcopy(case['positive']);detail='本次证据没有营业成本数据。'
    body.update(status='partial',missing_points=[detail],answer_parts=[dict(
        part_id='p1',state='partial',fact_indices=[0],missing_detail=detail)])
    env.model.handler=lambda *_:json.dumps(upgrade(body, {1: case['source']},strict=True),ensure_ascii=False)
    job,_=submit(env,question='营业收入与成本是多少？')
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    done=env.service.get_job(job['id']);assert done['status']=='succeeded',done
    answer=env.service.get_result(done['result_id'])['answer']
    assert answer['status']=='partial' and answer['answer_parts'][0]['state']=='partial'
    assert detail in env.service.export(done['result_id'],'markdown')[0]
    assert len(env.model.calls)==len(env.embedding.calls)==1


@pytest.mark.parametrize('source,text,unit,accepted',[
    ('统计期间2024-2025年。','统计2025年数据。','年',True),
    ('利率区间5%-10%。','利率上限10%。','%',True),
    ('亏损10万元。','亏损−10万元。','万元',False),
    ('亏损−10万元。','亏损−10万元。','万元',True),
])
def test_signed_numbers_do_not_turn_date_ranges_or_percentage_ranges_negative(source,text,unit,accepted):
    case=copy.deepcopy(make_cases()[0]);case['source']=source
    body=case['positive'];body['conclusion'][0].update(text=text,context=[dict(kind='unit',text=unit,ref=1)])
    if accepted:validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question=case['question'])
    else:
        with pytest.raises(ModelOutputError,match='number_not_in_quote'):
            validate_task_output(json.dumps(body,ensure_ascii=False),pack_for(case),question=case['question'])
