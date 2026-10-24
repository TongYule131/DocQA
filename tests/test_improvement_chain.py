"""S2→S5 的工程边界；全部合成输入，不访问真实模型或正式数据。"""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.answer_feedback import Feedback, Submit, Review
from app.conversation_memory import valid_memory, retrieval_question, MEMORY_VERSION
from app.entity_memory import context, edit
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_tasks import KnowledgeTasks
from app.knowledge_worker import KnowledgeWorker
from app.query_understanding import build_memory, understand, validate_plan
from app.resource_governance import admit, choose, provider_guard, owner_scope
from app.retrieval_channels import ChannelResult, apply_reranker, fingerprint
from app.index_cache import VectorCache
from tests.test_knowledge_chain import library
from tests.test_question_jobs import LocalRuntime


def test_multichannel_identity_survives_fusion_and_global_admission(tmp_path):
    env,access,base=library(tmp_path)
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'new')
    task=service.submit(conv['id'],'退货包装有什么要求','q')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id'])
    diagnostic=service.tasks.checkpoint(task['id'],'diagnostic_retrieval_attempt_1')
    assert diagnostic
    all_candidates=[]
    for doc in diagnostic['documents']:
        assert 'vector' in doc['channels'] and doc['channels']['vector']>0
        assert doc['admitted_count']==sum(i['document_id']==doc['document_id'] for i in diagnostic['input_manifest'])
        for candidate in doc['fusion']['candidates']:
            assert candidate['document_id']==doc['document_id']
            assert all(h['parse_version_id']==doc['parse_version_id'] for h in candidate['hits'])
            all_candidates.append(candidate)
    assert any(len(c['hits'])>1 for c in all_candidates)


def test_reranker_cannot_insert_or_remove_sources_or_displace_protected_group():
    pool=[{'chunk_id':str(i),'text':str(i)} for i in range(4)]
    class Reverse:
        version='synthetic'
        def rank(self,q,c):return ['3','2','1','0']
    result,report=apply_reranker('test',pool,Reverse(),['0','1'])
    assert [c['chunk_id'] for c in result]==['0','1','3','2']
    class Bad(Reverse):
        def rank(self,q,c):return ['unknown']
    result,report=apply_reranker('test',pool,Bad())
    assert result==pool and report['state']=='fallback_original'
    assert not report['semantic_gain_verified']


@pytest.mark.parametrize('score',[float('nan'),float('inf'),True])
def test_channel_rejects_nonfinite_or_boolean_score(score):
    with pytest.raises(ValueError):ChannelResult('d','v','c','vector',score,1,'test')


def test_long_memory_keeps_early_entities_and_sources_without_assistant_text():
    history=[{'turn':1,'question':'请比较“机制甲”与“机制乙”的差异'}]+[
        {'turn':i,'question':'继续解释条件'} for i in range(2,12)]
    value=build_memory(history,summary_enabled=True)
    assert {e['name'] for e in value['entities']}=={'机制甲','机制乙'}
    assert value['summary']['source_turns']==list(range(1,9))
    memory={'version':MEMORY_VERSION,'notes':'','recent_questions':[], 'entity_context':value}
    assert valid_memory(memory)
    assert '机制甲' in retrieval_question('两者的前提呢',memory)
    assert understand('两者的前提呢',value,SimpleNamespace(query_term_mapping='{}'))['original_question']=='两者的前提呢'


def test_correction_invalidates_old_entity_clues_and_summary():
    value=build_memory([{'turn':1,'question':'研究“旧对象”'}, {'turn':2,'question':'更正，改为研究“新对象”'}],summary_enabled=True)
    assert [e['name'] for e in value['entities']]==['新对象']
    assert value['summary'] is None


def test_query_plan_rejects_invented_numbers_entities_and_scope():
    good={'search_terms':['条件'],'subquestions':[]}
    assert validate_plan(good,'请解释条件',{})==good
    for value in ({'search_terms':['20%'],'subquestions':[]}, {'search_terms':['扩大至所有知识库'],'subquestions':[]},
                  {'search_terms':[],'subquestions':[],'scope':'all'}):
        with pytest.raises(ValueError):validate_plan(value,'请解释条件',{})


def test_entity_edit_epoch_clear_and_submission_freeze(tmp_path):
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'new')
    edited=edit(service,conv['id'],['比较对象甲','比较对象乙'],conv['revision'])
    task=service.submit(conv['id'],'两者有何区别','q')
    payload=json.loads(service.tasks.row(task['id'])['input_json'])
    assert {e['name'] for e in payload['conversation_context']['entity_context']['entities']}=={'比较对象甲','比较对象乙'}
    with pytest.raises(HTTPException):edit(service,conv['id'],['偷换对象'],edited['revision'])
    with env.repository.connect() as db:db.execute("UPDATE kb_tasks SET status='cancelled' WHERE id=?",(task['id'],))
    service.memory(conv['id'],False,True)
    current=service.require(conv['id'])
    with env.repository.connect() as db:assert context(db,current,env.settings)['entities']==[]
    assert payload['understanding']['scope_policy']=='authorized_selected_snapshot'


def test_optional_query_planner_is_accounted_once_and_invalid_output_falls_back(tmp_path):
    env,access,base=library(tmp_path);settings=replace(env.settings,query_planner_enabled=True)
    access.settings=settings;env.rag.settings=settings
    service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'new')
    env.model.handler=lambda system,user: '{}'
    task=service.submit(conv['id'],'退货要求','q')
    worker=KnowledgeWorker(settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id'])
    assert service.tasks.checkpoint(task['id'],'query_understanding')=={'search_terms':[],'subquestions':[]}
    with env.repository.connect() as db:
        roles=[r[0] for r in db.execute('SELECT role FROM kb_task_calls WHERE task_id=? ORDER BY sequence_no',(task['id'],))]
    assert roles==['query_planning','query_embedding','generation']
    assert len(env.model.calls)==2 and len(env.embedding.calls)==1


def test_feedback_privacy_idempotency_workflow_and_revocation(tmp_path):
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'new')
    task=service.submit(conv['id'],'私有问题标识','q')
    feedback=Feedback(access,service,env)
    body=Submit(source='knowledge',task_id=task['id'],request_key='feedback-key',category='expression',comment='私有说明')
    saved=feedback.submit(body)
    assert feedback.submit(body)['id']==saved['id']
    admin=json.dumps(feedback.list(admin=True),ensure_ascii=False)
    assert '私有说明' not in admin and '私有问题标识' not in admin
    assert 'shared_context' not in feedback.get(saved['id'],admin=True)
    with pytest.raises(HTTPException):feedback.review(saved['id'],Review(expected_revision=1,status='closed'))
    with pytest.raises(HTTPException):feedback.review(saved['id'],Review(expected_revision=1,status='reproduced'))
    row=feedback.review(saved['id'],Review(expected_revision=1,status='reproduced',root_cause='presentation'))
    row=feedback.review(saved['id'],Review(expected_revision=row['revision'],status='fixed',regression_case='synthetic-case-1'))
    with pytest.raises(HTTPException):feedback.review(saved['id'],Review(expected_revision=row['revision'],status='verified',original_retest='run-1'))
    row=feedback.review(saved['id'],Review(expected_revision=row['revision'],status='verified',original_retest='run-1',negative_retest='negative-run-2'))
    assert feedback.review(saved['id'],Review(expected_revision=row['revision'],status='closed'))['status']=='closed'
    assert not env.model.calls and not env.embedding.calls


def test_queue_rejection_is_atomic_and_idempotent_reuse_remains_available(tmp_path):
    env,access,base=library(tmp_path);env.repository.settings=replace(env.settings,queue_user_limit=1)
    service=KnowledgeQuestions(access,env.rag);a=service.create(base['id'],'a');b=service.create(base['id'],'b')
    first=service.submit(a['id'],'问题甲','one')
    assert service.submit(a['id'],'问题甲','one')['id']==first['id']
    with pytest.raises(HTTPException) as error:service.submit(b['id'],'问题乙','two')
    assert error.value.status_code==429
    with env.repository.connect() as db:assert db.execute('SELECT COUNT(*) FROM kb_tasks').fetchone()[0]==1
    assert not env.model.calls and not env.embedding.calls


def test_active_quota_and_user_rotation(tmp_path):
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag)
    a=service.create(base['id'],'a');b=service.create(base['id'],'b')
    first=service.submit(a['id'],'问题甲','one');second=service.submit(b['id'],'问题乙','two')
    tasks=KnowledgeTasks(env.repository);claimed=tasks.claim('worker',30,first['id']);assert claimed
    assert tasks.claim('another',30,second['id']) is None
    tasks.finish(claimed,'cancelled')
    assert tasks.claim('another',30,second['id'])


def test_circuit_opens_without_extra_call_and_daily_limit_counts_failed_attempts(tmp_path):
    env,_,_=library(tmp_path)
    class Error(Exception):
        def __init__(self,status_code,message):super().__init__(message);self.status_code=status_code
    settings=replace(env.settings,circuit_failure_threshold=2,calls_user_daily_limit=3)
    with owner_scope('user-one'):
        for _ in range(2):
            with pytest.raises(Error):
                with provider_guard(settings,'deepseek',Error):raise Error(504,'timeout')
        with pytest.raises(Error,match='熔断'):
            with provider_guard(settings,'deepseek',Error):pytest.fail('must not send')
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM provider_guard_calls').fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM provider_guard_calls WHERE status='uncertain'").fetchone()[0]==2


def test_vector_cache_rechecks_bytes_identity_dimension_and_bound():
    cache=VectorCache();rows=[('c','[1,0]')]
    _,hit=cache.decode(('scope','index-v1'),rows,2,1024);assert not hit
    _,hit=cache.decode(('scope','index-v1'),rows,2,1024);assert hit
    value,hit=cache.decode(('scope','index-v1'),[('c','[0,1]')],2,1024)
    assert not hit and value['c']==(0,1)
    with pytest.raises(ValueError):cache.decode(('scope','index-v1'),rows,3,1024)
    cache.decode(('new',),rows,2,0);assert cache.size==0


def test_backup_restore_to_new_directory_and_reject_modified_manifest(tmp_path):
    source=tmp_path/'source';source.mkdir()
    env,_,_=library(source)
    from app.data_backup import backup,restore
    private=env.settings.data_dir/'private-source.txt';private.write_text('合成原文',encoding='utf-8')
    target=tmp_path/'backup-snapshot';restored=tmp_path/'restored'
    manifest=backup(env.settings.data_dir,target)
    assert any(e['path']=='private-source.txt' for e in manifest['files'])
    result=restore(target,restored)
    assert result['provider_calls']==0 and not result['services_started']
    assert (restored/'private-source.txt').read_text(encoding='utf-8')=='合成原文'
    with pytest.raises(ValueError):restore(target,env.settings.data_dir)
    (target/'private-source.txt').write_text('篡改',encoding='utf-8')
    with pytest.raises(ValueError):restore(target,tmp_path/'invalid-restore')
    assert not (tmp_path/'invalid-restore').exists()
