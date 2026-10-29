"""N03真实执行器的发送前身份／封存／逐题复核边界，全部离线。"""
from hashlib import sha256
from types import SimpleNamespace
import json
import pytest
from scripts.evaluate_markdown_live import load_labels,require_previous,EmbeddingSpy,ModelSpy,verify_frozen

def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v),'utf-8')
def h(p):return sha256(p.read_bytes()).hexdigest()

def sealed(tmp_path):
    label=tmp_path/'candidate.json';auth=tmp_path/'user-authorization.json'
    write(label,{'sealed':True,'review_status':'accepted_by_user','cases':[]});write(auth,{'accepted':True})
    manifest=tmp_path/'sealed-manifest.json';write(manifest,{'status':'sealed','candidate_file':label.name,'candidate_sha256':h(label),
        'user_authorization_file':auth.name,'user_authorization_sha256':h(auth)})
    return manifest,label,auth

@pytest.mark.parametrize('change',['pending','label','authorization'])
def test_n03_seal_cannot_be_inferred_from_an_unapproved_or_changed_document(tmp_path,change):
    manifest,label,auth=sealed(tmp_path);assert load_labels(manifest)['sealed']
    if change=='pending':value=json.loads(manifest.read_text());value['status']='pending';write(manifest,value)
    elif change=='label':write(label,{'sealed':False})
    else:write(auth,{'accepted':False})
    with pytest.raises(ValueError,match='seal_required'):load_labels(manifest)

def test_next_paid_step_requires_a_semantic_pass_bound_to_exact_previous_result(tmp_path):
    result=tmp_path/'steps/ask-old-test/result.json';review=result.with_name('semantic-review.json')
    write(result,{'engineering_passed':True});plan={'steps':['ask-old-test','index-new']}
    with pytest.raises(FileNotFoundError):require_previous(tmp_path,plan,'index-new')
    write(review,{'passed':True,'result_sha256':h(result)});require_previous(tmp_path,plan,'index-new')
    write(result,{'engineering_passed':True,'different_answer':True})
    with pytest.raises(ValueError,match='semantic_review_required'):require_previous(tmp_path,plan,'index-new')

@pytest.mark.parametrize('operation,texts',[('index',['changed source']),('query',['different question'])])
def test_changed_embedding_inputs_do_not_reach_the_provider(operation,texts):
    runtime=SimpleNamespace(settings=SimpleNamespace(),operation=operation,phase='old',case={'id':'sealed','question':'frozen question'},
        plan={'index_texts':{'old':['frozen source']}})
    spy=EmbeddingSpy(runtime);spy.provider=SimpleNamespace(embed=lambda _:pytest.fail('must not send'))
    with pytest.raises(ValueError,match='input_changed'):spy.embed(texts)
    assert spy.calls==[]

def test_unallocated_generation_never_reaches_the_provider():
    runtime=SimpleNamespace(settings=SimpleNamespace(),phase='new',case={'id':'sealed','question':'frozen question'},plan={'http_slots':{}})
    spy=ModelSpy(runtime);spy.provider=SimpleNamespace(generate=lambda *_:pytest.fail('must not send'))
    with pytest.raises(ValueError,match='unallocated_model_operation'):spy.generate('system',json.dumps({'question':'frozen question'}))
    assert spy.calls==[]

def test_changed_application_fails_before_opening_any_runtime_or_budget(tmp_path):
    write(tmp_path/'plan.json',{'application_source_identity':'unrelated'})
    with pytest.raises(ValueError,match='application_changed'):verify_frozen(tmp_path)


def test_query_guard_accepts_the_frozen_production_context_wrapper_without_changing_it():
    expected='frozen question\n用户会话上下文（不是证据）：\n\n';sent=[]
    runtime=SimpleNamespace(settings=SimpleNamespace(),operation='query',phase='old',case={'id':'sealed','question':'frozen question'},expected_query=expected)
    spy=EmbeddingSpy(runtime);spy.provider=SimpleNamespace(embed=lambda texts:sent.append(texts) or [[1.0,0.0]])
    assert spy.embed([expected])==[[1.0,0.0]] and sent==[[expected]]


def test_mock_rehearsal_is_rejected_on_the_real_unique_ledger(tmp_path):
    from app.runtime import source_identity,code_fingerprint
    write(tmp_path/'plan.json',{'application_source_identity':source_identity(),'code_fingerprint':code_fingerprint(),'provider_mode':'offline_http_mock'})
    with pytest.raises(ValueError,match='mock_cannot_use_unique_live_ledger'):verify_frozen(tmp_path)


@pytest.mark.parametrize('code,allowed',[('output_numeric_context_missing',True),('call_uncertain',False),('source_changed',False)])
def test_independent_profile_preserves_output_failures_but_never_continues_network_or_source_failures(tmp_path,code,allowed):
    result=tmp_path/'steps/ask-old-test/result.json';write(result,{'engineering_passed':False,'task':{'error_code':code}})
    plan={'steps':['ask-old-test','index-new'],'independent_case_failures':True}
    if allowed:require_previous(tmp_path,plan,'index-new');assert json.loads(result.read_text())['engineering_passed'] is False
    else:
        with pytest.raises(ValueError,match='previous_step_failed'):require_previous(tmp_path,plan,'index-new')


def test_independent_profile_keeps_an_unexpected_successful_partial_answer_failed(tmp_path):
    result=tmp_path/'steps/ask-old-test/result.json';write(result,{'engineering_passed':False,'task':{'status':'succeeded'}})
    require_previous(tmp_path,{'steps':['ask-old-test','index-new'],'independent_case_failures':True},'index-new')
    assert json.loads(result.read_text())['engineering_passed'] is False
