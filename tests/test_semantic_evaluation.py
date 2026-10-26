from copy import deepcopy
import pytest
from fastapi import HTTPException
from app.semantic_evaluation import freeze_suite,select_policy,evaluate_holdout,validate_suite
from app.semantic_intents import rank_cached_examples
from app.retrieval_channels import fingerprint


def inputs():
    from hashlib import sha256
    rows=[{'id':code,'code':code,'name':name,'kind':'KB','parent_id':None,'enabled':True,'base_id':'base',
        'revision':1,'examples':[example],'keywords':[],'match_mode':'exact','tool_id':None,'top_k':None,'response_text':''}
        for code,name,example in [('returns','退货','办理退货'),('invoice','发票','申请开票')]]
    cache={r['id']:[{'example_hash':sha256(r['examples'][0].encode()).hexdigest(),'vector':vector}]
        for r,vector in zip(rows,([1.,0.],[0.,1.]))}
    cases=[]
    for split in ('calibration','holdout'):
        for i,(code,vector) in enumerate([('returns',[1.,0.]),('returns',[1.,0.]),('invoice',[0.,1.]),('invoice',[0.,1.]),('knowledge-default',[-1.,-1.]),('knowledge-default',[-1.,-1.])]):
            cases.append({'id':f'{split}-{i}','question':f'{split} 的合成业务问题{i}','split':split,'document_group':split+'-family',
                'tag':'business' if code!='knowledge-default' else 'fallback','base_id':'base','scope':'knowledge',
                'expected_code':code,'expected_kind':'KB','context':{},'query_vector':vector})
    return rows,cache,cases


def suite():return freeze_suite(*inputs(),'synthetic-2d')


def test_expected_source_unavailability_is_not_a_successful_live_answer_or_smalltalk():
    rows,cache,cases=inputs()
    for split in ('calibration','holdout'):
        target=next(c for c in cases if c['split']==split)
        target.update(question=('请' if split=='calibration' else '麻烦')+'查询今天仓库库存',
                      expected_code='live-source-unavailable',expected_kind='SYSTEM',tag='mixed_business',query_vector=None)
    frozen=freeze_suite(rows,cache,cases,'synthetic-2d');policy=select_policy(frozen)
    report=evaluate_holdout(frozen,policy)
    assert report['holdout']['correct']==6 and report['holdout']['safety']['business_diverted_to_system']==0


def test_selection_reads_only_calibration_and_never_writes_business_policy(monkeypatch):
    import app.semantic_evaluation as module
    frozen=suite();original=module._evaluate;seen=[]
    def recording(bundle,cases,*args):
        seen.extend(c['split'] for c in cases);return original(bundle,cases,*args)
    monkeypatch.setattr(module,'_evaluate',recording);policy=select_policy(frozen)
    assert set(seen)=={'calibration'} and policy['calibration']['correct']==6
    assert policy['threshold']==0.6 and policy['margin']==0.15
    report=evaluate_holdout(frozen,policy)
    assert report['holdout']['correct']==6 and report['holdout']['accuracy_target_met']
    assert not report['holdout']['sample_adequate'] and not report['holdout']['target_met']
    assert not report['formal_policy_written'] and not report['provider_calls']
    assert not report['holdout']['independent_human_certification']


@pytest.mark.parametrize('change,reason',[
    ('group','split_group_leak'),('duplicate','duplicate_or_training_example_leak'),
    ('example','duplicate_or_training_example_leak'),('dimension','semantic_vector_dimension_mismatch'),
    ('cache','semantic_examples_incomplete')])
def test_invalid_negative_examples_cannot_inflate_correct_fallback(change,reason):
    rows,cache,cases=inputs()
    if change=='group':cases[-1]['document_group']=cases[0]['document_group']
    if change=='duplicate':cases[-1]['question']=cases[0]['question']+'。'
    if change=='example':cases[-1]['question']='办理退货。'
    if change=='dimension':cases[-1]['query_vector']=[0.,0.,1.]
    if change=='cache':cache['invoice']=[]
    with pytest.raises(ValueError,match=reason):freeze_suite(rows,cache,cases,'synthetic-2d')


def test_changed_vector_label_policy_and_source_reject_same_replay_identity():
    frozen=suite();policy=select_policy(frozen)
    for key,value in [('query_vector',[0.,1.]),('expected_code','invoice')]:
        changed=deepcopy(frozen);changed['cases'][0][key]=value
        with pytest.raises(ValueError,match='suite_identity_mismatch'):evaluate_holdout(changed,policy)
    changed=deepcopy(policy);changed['threshold']=0.99
    with pytest.raises(ValueError,match='policy_identity_mismatch'):evaluate_holdout(frozen,changed)
    changed=deepcopy(policy);changed['threshold']=0.9;changed['policy_hash']=fingerprint({k:v for k,v in changed.items() if k!='policy_hash'})
    with pytest.raises(ValueError,match='policy_not_from_locked_calibration'):evaluate_holdout(frozen,changed)
    changed=deepcopy(frozen);changed['source_identity']='different';changed['input_hash']=fingerprint({k:v for k,v in changed.items() if k!='input_hash'})
    with pytest.raises(ValueError,match='suite_source_changed'):validate_suite(changed)


def test_high_overall_accuracy_cannot_hide_business_diverted_to_clarification():
    rows,cache,cases=inputs();held=[c for c in cases if c['split']=='holdout']
    for i in range(6,24):cases.append({**held[0],'id':f'held-extra-{i}','question':f'补充的独立业务表达{i}'})
    target=next(c for c in cases if c['split']=='holdout');target.update(question='这两种方法哪个更合适？',tag='mixed_business',query_vector=None)
    frozen=freeze_suite(rows,cache,cases,'synthetic-2d');report=evaluate_holdout(frozen,select_policy(frozen))['holdout']
    assert report['correct']==23 and report['total']==24 and report['accuracy']>0.95
    assert report['safety']['business_diverted_to_system']==1 and not report['target_met']


def test_shared_ranker_is_identical_but_strict_evaluation_rejects_wrong_dimension():
    rows,cache,_=inputs()
    assert rank_cached_examples(rows,[0.,0.,1.],cache)==[]
    with pytest.raises(ValueError,match='dimension'):rank_cached_examples(rows,[0.,0.,1.],cache,strict=True)
    assert rank_cached_examples(rows,[1.,0.],cache)[0]['code']=='returns'


def test_calibration_rejects_wrong_dimension_instead_of_counting_it_as_negative(tmp_path):
    from tests.test_knowledge_chain import library
    from tests.test_understanding_execution import business
    from app.semantic_intents import SemanticIntents
    env,access,base=library(tmp_path);manager,row=business(access,base);index=SemanticIntents(env.repository)
    class Embedding:
        signature=env.embedding.signature;settings=env.embedding.settings
        def embed(self,texts):return [[1.,0.] for _ in texts]
    index.prepare(manager.list(),Embedding())
    cases=[{'id':str(i),'base_id':base['id'],'document_group':str(i),'query_vector':[1.,0.] if i<3 else [0.,0.,1.],
        'expected_code':row['code'] if i<3 else 'knowledge-default'} for i in range(6)]
    with pytest.raises(HTTPException) as failed:index.calibrate(manager.list(),Embedding.signature,cases,threshold=.6,margin=.15)
    assert failed.value.status_code==422
    with env.repository.connect() as db:assert db.execute('SELECT COUNT(*) FROM intent_semantic_policy').fetchone()[0]==0
