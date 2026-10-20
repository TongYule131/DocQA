import copy
import pytest

from app.rag_scope_evaluation import ScopeEvaluationError,bind_alignment,evaluate_scoped_answer,validate_annotation
from tests.fixtures.rag_quality.scope_bindings import make_scope_cases
from scripts.evaluate_rag_scope import evaluate


@pytest.mark.parametrize('case',make_scope_cases(),ids=lambda c:c['case_id'])
def test_frozen_multi_document_scope_variants(case):
    for name,answer in [('positive',case['positive']),*case['variants'].items()]:
        result=evaluate_scoped_answer(case['annotation'],answer,bind_alignment(case['annotation'],answer,case['alignment']))
        if case['expected']=='unverified':assert result['status']=='unverified' and not result['retention_gate_passed']
        else:assert result['retention_gate_passed']==(name=='positive'),result
        assert result['semantic_accuracy'] is None


@pytest.mark.parametrize('mutation',['source_hash','offset','cross_fact','overlap','citation_version','quote','context','stale_alignment'])
def test_invalid_labels_or_stale_answers_cannot_receive_a_score(mutation):
    case=copy.deepcopy(make_scope_cases()[0]);a,answer=case['annotation'],case['positive']
    bound=bind_alignment(a,answer,case['alignment'])
    if mutation=='source_hash':a['sources'][0]['text']+='修改'
    if mutation=='offset':a['scopes'][0]['span']['start']+=1
    if mutation=='cross_fact':a['scopes'][0]['applies_to']=['another-document']
    if mutation=='overlap':a['scopes'][0]['excludes']=['primary']
    if mutation=='citation_version':answer['citations'][0]['parse_version_id']='wrong'
    if mutation=='quote':answer['citations'][0]['quote']='并非原文'
    if mutation=='context':answer['conclusion'][0]['context'][0]['text']='别处来源'
    if mutation=='stale_alignment':answer['conclusion'][0]['text']+='更改'
    if mutation!='stale_alignment':bound=bind_alignment(a,answer,case['alignment'])
    with pytest.raises(ScopeEvaluationError):evaluate_scoped_answer(a,answer,bound)


def test_missing_human_alignment_and_empty_answers_are_not_perfect_passes():
    case=make_scope_cases()[0]
    assert evaluate_scoped_answer(case['annotation'],case['positive'],None)['status']=='unreviewed'
    answer=dict(conclusion=[],explanation=[],citations=[])
    result=evaluate_scoped_answer(case['annotation'],answer,bind_alignment(case['annotation'],answer,[]))
    assert result['status']=='unverified' and not result['retention_gate_passed']


def test_global_note_does_not_repair_a_missing_qualification_in_another_fact():
    case=copy.deepcopy(make_scope_cases()[0]);a=case['annotation'];answer=case['variants']['omitted']
    phrase=a['scopes'][0]['span']['quote']
    a['facts'].append(dict(fact_id='global-note',anchor=a['scopes'][0]['span']))
    answer['explanation']=[dict(text=phrase,refs=[1],context=[])]
    assignments=case['alignment']+[dict(answer_index=2,source_fact_ids=['global-note'])]
    result=evaluate_scoped_answer(a,answer,bind_alignment(a,answer,assignments))
    assert result['status']=='failed'
    assert any(c['answer_index']==0 and c['result']=='missing_qualification' for c in result['checks'])


def test_compound_fact_across_opposing_scopes_requires_manual_review():
    case=make_scope_cases()[0];answer=copy.deepcopy(case['positive'])
    answer['conclusion']=answer['conclusion'][:1]
    assignments=[dict(answer_index=0,source_fact_ids=['primary','control'])]
    result=evaluate_scoped_answer(case['annotation'],answer,bind_alignment(case['annotation'],answer,assignments))
    assert result['status']=='unverified' and not result['retention_gate_passed']


def test_qualification_needs_its_source_in_the_same_facts_selected_quote():
    case=copy.deepcopy(make_scope_cases()[0]);answer=case['positive']
    phrase=case['annotation']['scopes'][0]['span']['quote']
    answer['conclusion'][0]['context'].pop()
    answer['conclusion'][0]['text']+='，'+phrase
    answer['citations'][0]['quote']='2025年营业收入1280万元'
    answer['conclusion']=answer['conclusion'][:1]
    answer['conclusion'][0]['context']=[]
    result=evaluate_scoped_answer(case['annotation'],answer,bind_alignment(case['annotation'],answer,case['alignment'][:1]))
    assert result['checks'][0]['result']=='missing_qualification_source'


def test_scope_fixture_guard_is_not_online_semantic_accuracy():
    report=evaluate()
    assert report['fixture_guard_passed'] and report['explicit_positive_passes']==11
    assert report['explicit_negative_rejections']==33 and report['unverified_outputs']==4
    assert report['provider_calls']==0 and report['semantic_accuracy'] is None


@pytest.mark.parametrize('ambiguous',[False,True])
def test_repeated_qualification_must_reference_its_annotated_occurrence(ambiguous):
    from app.rag_evaluation import digest
    case=copy.deepcopy(make_scope_cases()[0]);annotation=case['annotation'];answer=case['positive']
    text='2025年营业收入1280万元，未经审计。2024年营业收入1100万元，未经审计。'
    sid=annotation['sources'][0]['source_id']
    annotation['sources'][0].update(text=text,text_sha256=digest(text))
    for fact,phrase in zip(annotation['facts'],['2025年营业收入1280万元','2024年营业收入1100万元']):
        start=text.index(phrase);fact['anchor']=dict(source_id=sid,start=start,end=start+len(phrase),quote=phrase)
    start=text.index('未经审计')
    annotation['scopes'][0].update(span=dict(source_id=sid,start=start,end=start+4,quote='未经审计'),contradictions=[])
    answer['conclusion']=answer['conclusion'][:1]
    answer['conclusion'][0]['context']=[dict(kind='condition',text='未经审计',ref=2)]
    answer['conclusion'][0]['refs']=[1,2]
    answer['citations'][0]['quote']='2025年营业收入1280万元'
    answer['citations'].append({**answer['citations'][0],'reference_id':2,
        'quote':'未经审计' if ambiguous else '2024年营业收入1100万元，未经审计。'})
    result=evaluate_scoped_answer(annotation,answer,bind_alignment(annotation,answer,case['alignment'][:1]))
    assert not result['retention_gate_passed']
    assert result['checks'][0]['result']==('unverified_scope' if ambiguous else 'missing_qualification_source')
