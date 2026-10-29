"""显式阶段授权与唯一账本：全部使用本地MockTransport，不连接网络。"""
import json
import sqlite3
import gzip
from hashlib import sha256
import time

import httpx
import pytest

from app.evaluation_budget import EvaluationStopped
from scripts.approved_live_calls import ApprovedCalls,MeteredTransport,purpose,hashed


def budget(tmp_path):
    path=tmp_path/'ledger.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE approval(id INTEGER PRIMARY KEY,max_requests INTEGER,fingerprint TEXT)')
        db.execute("INSERT INTO approval VALUES (1,328,'old-closed-plan')")
        db.execute('CREATE TABLE calls(sequence INTEGER PRIMARY KEY,role TEXT,context TEXT,request_sha256 TEXT,status TEXT,started_at TEXT,settled_at TEXT,elapsed_ms INTEGER,http_status INTEGER,metadata_json TEXT)')
        db.execute("INSERT INTO calls(sequence,role,context,status) VALUES (1,'generation','historical','uncertain')")
    plan={'stage_id':'unit-explicit','start_sequence':1,'previous_approval':[328,'old-closed-plan'],
        'max_model_http':80,'http_slots':{'known':1},'allowed_urls':['https://isolated.invalid/chat/completions']}
    value=ApprovedCalls(path,plan,{'max_model_http':80,'used_at_approval':1,'scope':'N01-N02-current-run'},tmp_path/'raw')
    value.activate();return value


def test_legacy_uncertainty_is_preserved_and_fresh_authorization_does_not_add_the_old_six(tmp_path):
    b=budget(tmp_path)
    with httpx.Client(transport=MeteredTransport(b,httpx.MockTransport(lambda r:httpx.Response(200,json={'usage':{'total_tokens':2},'choices':[{'finish_reason':'stop'}]})))) as client:
        with purpose('known'):assert client.post('https://isolated.invalid/chat/completions',json={'synthetic':True},headers={'Authorization':'Bearer unit-secret'}).status_code==200
        with purpose('known'),pytest.raises(EvaluationStopped):client.post('https://isolated.invalid/chat/completions',json={})
    with b.connect() as db:
        assert db.execute('SELECT max_requests FROM approval').fetchone()[0]==81
        assert db.execute('SELECT status FROM calls WHERE sequence=1').fetchone()[0]=='uncertain'
        assert db.execute('SELECT status FROM calls WHERE sequence=2').fetchone()[0]=='succeeded'
        assert json.loads(db.execute('SELECT previous_approval_json FROM evaluation_stage_approvals').fetchone()[0])==[328,'old-closed-plan']
    assert b.report()['used']==1
    assert all(b'unit-secret' not in p.read_bytes() for p in b.output.iterdir())


@pytest.mark.parametrize('method,url,name',[('GET','https://isolated.invalid/chat/completions','known'),
    ('POST','https://other.invalid/chat/completions','known'),('POST','https://isolated.invalid/chat/completions','unknown')])
def test_unfrozen_destinations_methods_or_purposes_fail_before_reservation(tmp_path,method,url,name):
    b=budget(tmp_path)
    with httpx.Client(transport=MeteredTransport(b,httpx.MockTransport(lambda r:pytest.fail('must not send')))) as client:
        with purpose(name),pytest.raises(EvaluationStopped):client.request(method,url,json={})
    assert b.report()['used']==0


def test_transport_uncertainty_persists_a_stop_and_is_never_retried(tmp_path):
    b=budget(tmp_path);sent=[]
    def fail(r):sent.append(r);raise httpx.ReadError('synthetic disconnect')
    with httpx.Client(transport=MeteredTransport(b,httpx.MockTransport(fail))) as client:
        with purpose('known'),pytest.raises(httpx.ReadError):client.post('https://isolated.invalid/chat/completions',json={})
        with purpose('known'),pytest.raises(EvaluationStopped):client.post('https://isolated.invalid/chat/completions',json={})
    assert len(sent)==1 and b.report()['calls'][0]['status']=='uncertain'
    with b.connect() as db:assert db.execute('SELECT status FROM evaluation_stage_approvals').fetchone()[0]=='stopped'


def test_partial_stream_retains_the_first_bytes_and_marks_unknown_delivery(tmp_path):
    b=budget(tmp_path)
    class Partial(httpx.SyncByteStream):
        def __iter__(self):yield b'data: partial\n';raise httpx.ReadError('interrupted')
    with httpx.Client(transport=MeteredTransport(b,httpx.MockTransport(lambda r:httpx.Response(200,stream=Partial())))) as client:
        with purpose('known'),pytest.raises(httpx.ReadError):client.post('https://isolated.invalid/chat/completions',json={})
    assert b.report()['calls'][0]['status']=='uncertain'
    assert (b.output/'2.response.bin').read_bytes()==b'data: partial\n'


@pytest.mark.parametrize('compressed',[False,True])
def test_sdk_close_after_verified_sse_done_preserves_usage_without_false_unknown(tmp_path,compressed):
    b=budget(tmp_path)
    data=b'data: {"id":"synthetic","choices":[{"finish_reason":"stop"}]}\n\ndata: {"choices":[],"usage":{"total_tokens":2}}\n\ndata: [DONE]\n\n'
    body=gzip.compress(data)[:-8] if compressed else data
    class Terminal(httpx.SyncByteStream):
        def __iter__(self):
            yield body
            pytest.fail('SDK already has the terminal event; no further read is needed')
    with httpx.Client(transport=MeteredTransport(b,httpx.MockTransport(lambda r:httpx.Response(200,stream=Terminal())))) as client:
        with purpose('known'),client.stream('POST','https://isolated.invalid/chat/completions',json={}) as response:
            next(response.iter_raw())
    assert b.report()['calls'][0]['status']=='succeeded'
    with b.connect() as db:assert json.loads(db.execute('SELECT metadata_json FROM calls WHERE sequence=2').fetchone()[0])['usage']['total_tokens']==2


def test_continuation_keeps_the_original_80_cap_and_old_uncertain_row_and_never_repeats_spent_slot(tmp_path):
    b=budget(tmp_path)
    with purpose('known'):sequence=b.reserve(httpx.Request('POST','https://isolated.invalid/chat/completions',json={}))
    body=b'data: {"choices":[{"finish_reason":"stop"}],"usage":{"total_tokens":2}}\n\ndata: [DONE]\n\n'
    b.settle(sequence,body,200,time.perf_counter(),uncertain=True)
    evidence=tmp_path/'verified.json';evidence.write_text(json.dumps({'sequence':sequence}),'utf-8')
    plan={**b.plan,'stage_id':'continued','activation_sequence':2,'previous_stage_ids':[b.stage],
        'previous_approval':[81,hashed(b.plan)],'http_slots':{'known':1,'next':1},
        'completed_stream_proofs':{'2':{'response_file':str(b.output/'2.response.bin'),'response_sha256':sha256(body).hexdigest(),
            'evidence_file':str(evidence),'evidence_sha256':sha256(evidence.read_bytes()).hexdigest()}}}
    continued=ApprovedCalls(b.path,plan,b.authorization,tmp_path/'continued-raw');continued.activate()
    with purpose('known'),pytest.raises(EvaluationStopped):continued.reserve(httpx.Request('POST','https://isolated.invalid/chat/completions',json={}))
    with purpose('next'):assert continued.reserve(httpx.Request('POST','https://isolated.invalid/chat/completions',json={}))==3
    with b.connect() as db:
        assert db.execute('SELECT max_requests FROM approval').fetchone()[0]==81
        assert db.execute('SELECT status FROM calls WHERE sequence=2').fetchone()[0]=='uncertain'
        assert db.execute('SELECT sequence FROM evaluation_call_reconciliations').fetchone()[0]==2


def test_new_n03_grant_starts_at_actual_usage_without_carrying_closed_unused_calls(tmp_path):
    previous=budget(tmp_path);previous.stop('complete')
    plan={**previous.plan,'stage_id':'n03-fresh','scope':'N03-current-run','previous_approval':[81,hashed(previous.plan)]}
    approval={'max_model_http':80,'used_at_approval':1,'scope':'N03-current-run'}
    current=ApprovedCalls(previous.path,plan,approval,tmp_path/'new-raw');current.activate()
    with current.connect() as db:
        assert db.execute('SELECT max_requests FROM approval').fetchone()[0]==81
        assert db.execute("SELECT status FROM evaluation_stage_approvals WHERE stage_id=?",(previous.stage,)).fetchone()[0]=='complete'
        assert db.execute('SELECT status FROM calls WHERE sequence=1').fetchone()[0]=='uncertain'
    assert current.report()['used']==0


@pytest.mark.parametrize('plan_scope,authorization_scope',[('N03-current-run','N01-N02-current-run'),
    ('N01-N02-current-run','N03-current-run'),('unapproved','unapproved')])
def test_scope_cannot_borrow_a_different_stage_authorization(tmp_path,plan_scope,authorization_scope):
    previous=budget(tmp_path)
    with pytest.raises(EvaluationStopped):ApprovedCalls(previous.path,{**previous.plan,'scope':plan_scope},
        {**previous.authorization,'scope':authorization_scope},tmp_path/'rejected-raw')
