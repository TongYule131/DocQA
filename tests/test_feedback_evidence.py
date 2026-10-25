"""工单必须使用真实登记结构；过期或同输入的假独立负例不能关闭。"""
from hashlib import sha256
import json
from xml.etree import ElementTree

from fastapi import HTTPException
import pytest

from app.answer_feedback import Feedback,Submit,Review,Evidence
from app.feedback_cases import create_case,record_evidence,withdraw
from app.knowledge_questions import KnowledgeQuestions
from app.runtime import source_identity
from tests.test_knowledge_chain import library


def feedback(tmp_path,share=True):
    env,access,base=library(tmp_path);questions=KnowledgeQuestions(access,env.rag)
    conv=questions.create(base['id'],'new');task=questions.submit(conv['id'],'合成问题','question')
    service=Feedback(access,questions,env)
    row=service.submit(Submit(source='knowledge',task_id=task['id'],request_key='feedback',category='expression',share_context=share))
    return env,service,row


def proof(case,row,kind,fixture='independent'):
    identity=source_identity();hash_value=sha256(fixture.encode()).hexdigest()
    root=ElementTree.Element('testsuite',tests='1',failures='0',errors='0')
    properties=ElementTree.SubElement(root,'properties')
    for name,value in {'docqa.case_sha256':case['case_sha256'],'docqa.feedback_generation':str(row['generation']),
        'docqa.source_fingerprint':identity,'docqa.source_fingerprint_after':identity,'docqa.fixture_sha256':hash_value,'docqa.fixture_sha256_after':hash_value}.items():
        ElementTree.SubElement(properties,'property',name=name,value=value)
    ElementTree.SubElement(root,'testcase',name=kind+'_test',classname='synthetic_feedback_evidence_guard')
    return Evidence(case_id=case['id'],kind=kind,code_identity=identity,fixture_sha256=hash_value,
        report_xml=ElementTree.tostring(root,encoding='unicode'),test_name=kind+'_test',run_id='synthetic-offline-guard')


def test_registered_original_and_independent_negative_close_and_reopen(tmp_path):
    env,service,row=feedback(tmp_path)
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='reproduced',root_cause='presentation'))
    case=create_case(service,row['id']);row=service.get(row['id'],admin=True)
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='fixed',regression_case=case['id']))
    record_evidence(service,row['id'],proof(case,row,'original','original fixture'))
    record_evidence(service,row['id'],proof(case,row,'negative','independent negative fixture'))
    row=service.get(row['id'],admin=True)
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='verified'))
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='closed',client_reply='已完成原失败和独立负例复验。'))
    assert row['status']=='closed' and row['client_reply']
    old_case=case['id'];old_generation=row['generation']
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='open',root_cause='ranking'))
    assert row['generation']==old_generation+1 and row['regression_case'] is None and row['original_retest'] is None
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='reproduced',root_cause='ranking'))
    with pytest.raises(HTTPException):service.review(row['id'],Review(expected_revision=row['revision'],status='fixed',regression_case=old_case))
    assert not env.model.calls and not env.embedding.calls


def test_arbitrary_strings_and_same_fixture_are_not_independent_evidence(tmp_path):
    _,service,row=feedback(tmp_path)
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='reproduced',root_cause='retrieval'))
    with pytest.raises(HTTPException):service.review(row['id'],Review(expected_revision=row['revision'],status='fixed',regression_case='made-up'))
    case=create_case(service,row['id']);row=service.get(row['id'],admin=True)
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='fixed',regression_case=case['id']))
    record_evidence(service,row['id'],proof(case,row,'original','same fixture'))
    record_evidence(service,row['id'],proof(case,row,'negative','same fixture'))
    row=service.get(row['id'],admin=True)
    with pytest.raises(HTTPException):
        service.review(row['id'],Review(expected_revision=row['revision'],status='verified'))


def test_consent_and_report_source_are_rechecked(tmp_path):
    _,service,row=feedback(tmp_path,share=False)
    with pytest.raises(HTTPException):create_case(service,row['id'])
    with service.repository.connect() as db:
        db.execute('UPDATE answer_feedback SET share_context=1,context_json=? WHERE id=?',(json.dumps({'question':'synthetic','answer':None}),row['id']))
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='reproduced',root_cause='validation'))
    case=create_case(service,row['id']);row=service.get(row['id'],admin=True)
    row=service.review(row['id'],Review(expected_revision=row['revision'],status='fixed',regression_case=case['id']))
    invalid=proof(case,row,'original');invalid.report_xml=invalid.report_xml.replace(source_identity(),'0'*64)
    with pytest.raises(HTTPException):record_evidence(service,row['id'],invalid)
    with service.repository.connect() as db:withdraw(db,row['id'],service.owner)
    assert 'shared_context' not in service.get(row['id'],admin=True)
    with service.repository.connect() as db:
        assert db.execute('SELECT payload_json FROM feedback_cases WHERE id=?',(case['id'],)).fetchone()[0] is None
    with pytest.raises(HTTPException):record_evidence(service,row['id'],proof(case,row,'original'))


def test_feedback_pagination_and_status_filter(tmp_path):
    _,service,row=feedback(tmp_path)
    for i in range(3):service.submit(Submit(source='knowledge',task_id=row['task_id'],request_key='more-'+str(i),category='helpful'))
    first=service.list(admin=True,limit=2,offset=0);second=service.list(admin=True,limit=2,offset=2)
    assert len(first)==len(second)==2 and not {r['id'] for r in first}&{r['id'] for r in second}
    assert len(service.list(admin=True,status='open',category='helpful'))==3
