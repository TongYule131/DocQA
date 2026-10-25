import json,os
from app.resident_audit import current_logon_context,record,summarize
from app.local_supervisor import write_state


def state(logon='00000000:00012345'):
    return {'pid':123,'process_start_token':'windows:123456','code_fingerprint':'a'*64,'status':'ready',
        'logon_context':{'session_id':1,'logon_id':logon},'question':'private content must never appear','data_dir':'private path'}


def test_real_current_logon_identity_contains_only_opaque_ids():
    context=current_logon_context()
    if os.name=='nt':
        assert context and set(context)=={'session_id','logon_id'}
        assert type(context['session_id']) is int and ':' in context['logon_id']
    else:assert context is None


def test_only_changed_logon_with_matching_code_is_evidence_not_automatic_acceptance(tmp_path):
    directory=tmp_path/'.local-service'
    assert record(directory,'supervisor_started',state())
    assert record(directory,'supervisor_started',state())
    assert not summarize(tmp_path)['different_logon_context_observed']
    other=state('00000000:00023456');other['code_fingerprint']='b'*64
    assert record(directory,'supervisor_started',other)
    assert not summarize(tmp_path)['different_logon_context_observed']
    assert record(directory,'supervisor_started',state('00000000:00034567'))
    report=summarize(tmp_path,fingerprint='a'*64)
    assert report['different_logon_context_observed'] and not report['real_logoff_logon_verified']
    assert report['startup_count']==3 and not report['provider_calls']
    text=(directory/'resident-audit.jsonl').read_text('utf-8');assert 'private' not in text and 'question' not in text


def test_repeated_ready_poll_does_not_fill_audit_and_corrupt_state_does_not_block(tmp_path):
    directory=tmp_path/'.local-service';directory.mkdir();(directory/'supervisor.json').write_text('[]',encoding='utf-8')
    write_state(directory,state());write_state(directory,state())
    assert len(summarize(tmp_path)['events'])==1
    stopped=state();stopped['status']='stopped';write_state(directory,stopped)
    assert len(summarize(tmp_path)['events'])==2


def test_audit_failure_cannot_report_saved_or_interrupt_normal_state(tmp_path,monkeypatch):
    import app.resident_audit as audit
    monkeypatch.setattr(audit._AuditHandler,'emit',lambda *_:(_ for _ in ()).throw(OSError('synthetic disk issue')))
    assert not record(tmp_path/'x','supervisor_started',state())
    directory=tmp_path/'y';directory.mkdir();write_state(directory,state())
    assert json.loads((directory/'supervisor.json').read_text('utf-8'))['status']=='ready'


def test_invalid_or_injected_audit_lines_are_not_returned(tmp_path):
    directory=tmp_path/'.local-service';directory.mkdir();path=directory/'resident-audit.jsonl'
    path.write_text('{bad}\n'+json.dumps({'version':'resident-lifecycle-audit-v1','event':'supervisor_started','private':'secret'})+'\n',encoding='utf-8')
    result=summarize(tmp_path);assert result['invalid_records']==2 and not result['events']


def test_partial_append_is_not_treated_as_complete_session_evidence(tmp_path):
    directory=tmp_path/'.local-service';directory.mkdir();(directory/'resident-audit.jsonl').write_text('{"version":',encoding='utf-8')
    report=summarize(tmp_path)
    assert report['incomplete_tails']==1 and report['startup_count']==0 and not report['different_logon_context_observed']


def test_operations_exposes_only_session_counts_not_logon_tokens(tmp_path):
    from tests.test_question_jobs import question_fixture
    from app.main import create_app
    from fastapi.testclient import TestClient
    env=question_fixture(tmp_path);app=create_app(env.settings)
    with TestClient(app) as client:
        example=state('00000000:00abcdef');example['code_fingerprint']=app.state.runtime_guard.fingerprint
        assert record(tmp_path/'.local-service','supervisor_started',example)
        response=client.get('/api/admin/operations');assert response.status_code==200
        evidence=response.json()['resident_session_evidence'];assert evidence['startup_count']==1
        assert 'events' not in evidence and '00abcdef' not in response.text
        assert not evidence['real_logoff_logon_verified']
