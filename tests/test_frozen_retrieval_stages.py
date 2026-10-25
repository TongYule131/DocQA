"""各阶段必要组必须来自不同执行集合；不以块ID冒充有效引用。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.frozen_retrieval import freeze, evaluate, vector_results
from app.schemas import Chunk, SourceLocation
from app.vector_index import compute_signature, evidence_signature
from app.migrations import SIGNATURE_ALGO_VERSIONED
from app.retrieval_channels import ReplayReranker, fingerprint, apply_reranker


def case():
    chunks=[Chunk(id=f'c{i}',document_id='doc',parse_version_id='pv',text=f'材料第{i}段。相关内容及说明。',order_index=i,
        sources=[SourceLocation(format='txt',line_start=i+1,line_end=i+1)]) for i in range(10)]
    doc={'document_id':'doc','filename':'synthetic.txt','chunks':[c.model_dump() for c in chunks],
        'quality':{'quality_status':'ok','warnings':[]},'vectors':[{'chunk_id':c.id,'vector':[1.0,0.0]} for c in chunks],
        'snapshot':{'index_id':'index','parse_version_id':'pv','provider_signature':'synthetic',
            'source_signature_algo':SIGNATURE_ALGO_VERSIONED,'source_signature':compute_signature(chunks,'pv',SIGNATURE_ALGO_VERSIONED),
            'evidence_signature':evidence_signature(chunks)}}
    doc['vector_result']=vector_results(doc,[1.0,0.0],10)
    return {'id':'stage-loss','question':'介绍材料','document_group':'synthetic','annotation_status':'synthetic',
        'query_vector':[1.0,0.0],'documents':[doc], 'budget':{'recall_limit':10,'candidate_limit':1,'context_limit':1,'context_chars':24000},
        'required_groups':[{'all_of':['c9'],'required_ranges':{'c9':[[0,6]]}}]}


def test_recalled_group_can_be_lost_after_ranking():
    row=evaluate(freeze([case()]))['cases'][0]
    assert row['candidate_groups']==1 and row['post_rank_groups']==0
    assert row['visible_groups']==0


def test_query_vector_or_required_range_cannot_be_forged():
    item=case();item['query_vector']=[0.0,1.0]
    with pytest.raises(ValueError,match='vector_result'):freeze([item])
    item=case();item['required_groups'][0]['required_ranges']['c9']=[[0,9999]]
    with pytest.raises(ValueError,match='range'):freeze([item])
    item=case();item['required_groups']=[{'all_of':[]}]
    with pytest.raises(ValueError,match='empty'):freeze([item])


def test_quote_requires_exact_visible_bytes_and_required_ranges():
    item=case();item['required_groups']=[{'all_of':['c0'],'required_ranges':{'c0':[[0,6]]}}]
    item['validated_quotes']=[{'chunk_id':'c0','start':0,'end':6,'quote':item['documents'][0]['chunks'][0]['text'][:6]}]
    assert evaluate(freeze([item]))['cases'][0]['quoted_groups']==1
    item['validated_quotes'][0]['quote']='forged'
    with pytest.raises(ValueError,match='quote'):evaluate(freeze([item]))


def test_replay_uses_immutable_loaded_content_and_configuration_identity(tmp_path):
    import json
    candidates=[{'chunk_id':'a','text':'a'},{'chunk_id':'b','text':'b'}]
    key=fingerprint({'question':'q','candidates':candidates});path=tmp_path/'rank.json'
    path.write_text(json.dumps({'version':ReplayReranker.version,'rankings':{key:['b','a']}}))
    ranker=ReplayReranker(path)
    path.write_text('{}')
    assert [h['chunk_id'] for h in apply_reranker('q',candidates,ranker)[0]]==['b','a']
    with pytest.raises(ValueError,match='changed'):ReplayReranker(path,ranker.content_hash)
    suite=freeze([case()])
    with pytest.raises(ValueError,match='settings'):evaluate(suite,Settings(rag_min_score=0.5))


def test_suite_embeds_replay_instead_of_silently_disabling_after_file_disappears(tmp_path):
    from dataclasses import replace
    import json
    path=tmp_path/'replay.json';path.write_text(json.dumps({'version':ReplayReranker.version,'rankings':{}}))
    settings=replace(Settings(),reranker_replay_path=str(path))
    suite=freeze([case()],settings);path.unlink()
    assert suite['reranker']['sha256']
    assert evaluate(suite)['provider_calls']==0
    changed=deepcopy(suite);changed['reranker']=None;changed['input_hash']=fingerprint({k:v for k,v in changed.items() if k!='input_hash'})
    with pytest.raises(ValueError,match='reranker_missing'):evaluate(changed)


def test_actual_task_export_reuses_exact_query_input_and_never_calls_provider(tmp_path):
    from tests.test_knowledge_chain import library
    from tests.test_question_jobs import LocalRuntime
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    from app.frozen_retrieval import export_case
    env,access,base=library(tmp_path);questions=KnowledgeQuestions(access,env.rag);conv=questions.create(base['id'],'export')
    task=questions.submit(conv['id'],'退货有什么条件','q');worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id']);before=(len(env.embedding.calls),len(env.model.calls))
    result=export_case(env.repository,task['id'],required_groups=[{'all_of':['pc1']}],document_group='synthetic',annotation_status='synthetic')
    assert result['search_question']==questions.tasks.checkpoint(task['id'],'query_input')['search_question']
    assert evaluate(freeze([result],env.settings))['provider_calls']==0
    assert before==(len(env.embedding.calls),len(env.model.calls))
