"""结构化回归包→实际输入→JUnit→证据导入链路的原例与独立负例。"""
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from tests.test_knowledge_chain import library
from tests.test_question_jobs import LocalRuntime


def test_feedback_clarification_original(tmp_path,feedback_repro_input):
    fixture=feedback_repro_input['input'] if feedback_repro_input else {'question':'两者有什么区别','expected_clarification':True}
    assert fixture['expected_clarification'] is True
    if feedback_repro_input:
        import json
        assert json.loads(feedback_repro_input['bundle']['payload_json'])['context']['question']==fixture['question']
    env,access,base=library(tmp_path);questions=KnowledgeQuestions(access,env.rag);conv=questions.create(base['id'],'repro')
    task=questions.submit(conv['id'],fixture['question'],'original');worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task['id']);result=questions.present(questions.require_task(task['id']))
    assert result['result']['clarification']['needed_count']==2
    assert result['requests_used']==0 and not env.embedding.calls and not env.model.calls


def test_feedback_mixed_business_negative(tmp_path,feedback_repro_input):
    fixture=feedback_repro_input['input'] if feedback_repro_input else {'question':'谢谢，退货需要什么条件','expected_clarification':False}
    assert fixture['expected_clarification'] is False
    from app.intent_management import IntentManagement
    env,access,base=library(tmp_path);resolution=IntentManagement(env.repository).evaluate(fixture['question'],base['id'])
    assert resolution['route']['kind']=='KB' and not resolution['understanding']['ambiguous_reference']
    assert not env.embedding.calls and not env.model.calls
