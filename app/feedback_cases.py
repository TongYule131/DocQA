"""反馈回归包与JUnit复验证据；版本失效不删除历史，撤回分享收回正文。"""
from hashlib import sha256
import json
import re
from uuid import uuid4
from xml.etree import ElementTree

from fastapi import HTTPException
from app.knowledge_tasks import dump,fingerprint
from app.repository import now_iso
from app.runtime import source_identity


def event(db,identifier,actor,status,detail):
    db.execute('INSERT INTO feedback_events VALUES (?,?,?,?,?,?)',('fevent-'+uuid4().hex,identifier,actor,status,dump(detail),now_iso()))


def create_case(service,identifier):
    service.access.admin()
    with service.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM answer_feedback WHERE id=?',(identifier,)).fetchone()
        if not row or not row['share_context'] or not row['context_json']:raise HTTPException(409,'客户尚未分享本题复现材料')
        prior=db.execute('SELECT * FROM feedback_cases WHERE feedback_id=? AND generation=?',(identifier,row['generation'])).fetchone()
        if prior:return dict(prior)
        payload={'version':'feedback-case-v1','feedback_id':identifier,'generation':row['generation'],
            'source':row['source'],'task_id':row['task_id'],'context':json.loads(row['context_json']),
            'automatic_model_calls':0,'scope':'customer_consented_single_answer'}
        case_id='fcase-'+uuid4().hex;encoded=dump(payload);digest=fingerprint(payload)
        db.execute('INSERT INTO feedback_cases VALUES (?,?,?,?,?,?)',(case_id,identifier,row['generation'],digest,encoded,now_iso()))
        db.execute('UPDATE answer_feedback SET regression_case=?,revision=revision+1,updated_at=? WHERE id=?',(case_id,now_iso(),identifier))
        event(db,identifier,service.owner,row['status'],{'action':'case_created','case_id':case_id,'case_sha256':digest})
        return dict(db.execute('SELECT * FROM feedback_cases WHERE id=?',(case_id,)).fetchone())


def record_evidence(service,identifier,body):
    service.access.admin()
    if '<!DOCTYPE' in body.report_xml.upper() or '<!ENTITY' in body.report_xml.upper():raise HTTPException(422,'复验报告不能包含DTD或实体')
    try:root=ElementTree.fromstring(body.report_xml)
    except ElementTree.ParseError:raise HTTPException(422,'JUnit XML无效') from None
    cases=[c for c in root.iter('testcase') if c.get('name')==body.test_name or (c.get('classname','')+'::'+c.get('name',''))==body.test_name]
    if len(cases)!=1 or any(cases[0].find(k) is not None for k in ('failure','error','skipped')):raise HTTPException(422,'指定复验用例缺失、不唯一或未通过')
    properties={p.get('name'):p.get('value') for p in root.iter('property')}
    with service.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM answer_feedback WHERE id=?',(identifier,)).fetchone()
        case=db.execute('SELECT * FROM feedback_cases WHERE id=? AND feedback_id=?',(body.case_id,identifier)).fetchone()
        if not row or not case or not row['share_context'] or not case['payload_json']:raise HTTPException(409,'复现案例不存在或分享已撤回')
        if case['generation']!=row['generation'] or row['status'] not in ('fixed','verified'):raise HTTPException(409,'复验必须关联当前修复阶段')
        if row['fix_revision']!=body.code_identity or body.code_identity!=source_identity():raise HTTPException(409,'复验代码与当前修复版本不一致')
        expected={'docqa.case_sha256':case['case_sha256'],'docqa.source_fingerprint':body.code_identity,
            'docqa.source_fingerprint_after':body.code_identity,'docqa.fixture_sha256':body.fixture_sha256,
            'docqa.fixture_sha256_after':body.fixture_sha256,'docqa.feedback_generation':str(row['generation'])}
        if any(properties.get(k)!=v for k,v in expected.items()):raise HTTPException(422,'JUnit缺少匹配的案例、代码、输入或工单代次属性')
        identifier_e='fevidence-'+uuid4().hex
        summary={'passed':True,'tests':1,'failures':0,'errors':0,'skipped':0,'evidence_type':'reviewed_targeted_junit',
            'independent_semantic_certification':False}
        db.execute('INSERT INTO feedback_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifier_e,identifier,row['generation'],body.case_id,body.kind,body.code_identity,body.fixture_sha256,
             sha256(body.report_xml.encode()).hexdigest(),body.test_name,body.run_id,dump(summary),service.owner,now_iso()))
        field='original_retest' if body.kind=='original' else 'negative_retest'
        db.execute(f'UPDATE answer_feedback SET {field}=?,revision=revision+1,updated_at=? WHERE id=?',(identifier_e,now_iso(),identifier))
        event(db,identifier,service.owner,row['status'],{'action':'evidence_registered','evidence_id':identifier_e,'kind':body.kind})
    return {'id':identifier_e,**summary,'provider_calls':0}


def validate_references(db,row,merged,fix_revision):
    case=db.execute('SELECT * FROM feedback_cases WHERE id=? AND feedback_id=? AND generation=?',
        (merged['regression_case'],row['id'],row['generation'])).fetchone()
    if not case or not case['payload_json']:raise HTTPException(422,'回归样本须关联当前工单代次的实际案例')
    if row['status'] in ('fixed','verified') and fix_revision!=row['fix_revision']:raise HTTPException(409,'更换修复版本须先重新打开反馈')
    if fix_revision!=source_identity():raise HTTPException(409,'修复版本与当前源码不一致')
    if merged.get('original_retest') and merged.get('negative_retest'):
        evidence=[]
        for kind,key in (('original','original_retest'),('negative','negative_retest')):
            proof=db.execute('SELECT * FROM feedback_evidence WHERE id=? AND feedback_id=? AND generation=? AND case_id=? AND kind=? AND code_identity=?',
                (merged[key],row['id'],row['generation'],case['id'],kind,fix_revision)).fetchone()
            if not proof:raise HTTPException(422,'原失败与独立负例必须引用当前版本的已登记通过证据')
            evidence.append(proof)
        if evidence[0]['fixture_sha256']==evidence[1]['fixture_sha256'] or evidence[0]['test_name']==evidence[1]['test_name']:
            raise HTTPException(422,'独立负例不能复用原失败的同一输入或同一用例')


def withdraw(db,identifier,actor):
    db.execute('UPDATE answer_feedback SET share_context=0,context_json=NULL,revision=revision+1,updated_at=? WHERE id=?',(now_iso(),identifier))
    db.execute('UPDATE feedback_cases SET payload_json=NULL WHERE feedback_id=?',(identifier,))
    event(db,identifier,actor,'open',{'action':'context_withdrawn'})
