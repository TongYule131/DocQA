"""职责边界的独立工程负例；不是待审核候选集或产品准确率。"""
import json

import pytest

from app.intent_execution import request_requirements
from app.intent_management import IntentManagement
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from tests.test_knowledge_chain import library
from tests.test_question_jobs import LocalRuntime


@pytest.mark.parametrize('question,live,mixed', [
    ('请不要查询当前账户余额。', False, False),
    ('麻烦不要调用库存接口查询商品。', False, False),
    ('请先别查询今天的物流。', False, False),
    ('解释文档的退货规则以及查询今天的物流。', True, True),
    ('查询今天的库存然后解释报告的结论。', True, True),
    ('先总结报告再查看当前设备负载。', True, True),
    ('请解释报告中如何查询当前库存。', False, False),
    ('不要查询当前余额，然后查询今天的物流。', True, False),
])
def test_explicit_actions_keep_negation_and_unpunctuated_coordination(question, live, mixed):
    assert request_requirements(question) == {
        'requires_live_source': live, 'mixed_document_and_live': mixed}


@pytest.mark.parametrize('question,action,status', [
    ('解释文档规则以及查询今天的物流。', 'split_request', 'clarification_needed'),
    ('查询当前设备负载然后解释报告的结论。', 'split_request', 'clarification_needed'),
    ('查询今天的会议安排。', 'source_unavailable', 'insufficient_evidence'),
    ('谢谢您。', 'reply', 'answered'),
    ('这两种方法有什么区别？', 'clarify', 'clarification_needed'),
])
def test_bench_submission_worker_and_replay_have_the_same_zero_call_action(tmp_path, question, action, status):
    env, access, base = library(tmp_path)
    questions = KnowledgeQuestions(access, env.rag)
    conversation = questions.create(base['id'], 'responsibilities')
    task = questions.submit(conversation['id'], question, 'ask')
    payload = json.loads(questions.require_task(task['id'])['input_json'])
    bench = IntentManagement(env.repository).evaluate(
        question, base['id'], context=payload['conversation_context']['entity_context'])
    assert payload['execution'] == bench['execution']
    assert payload['execution']['action'] == action
    assert payload['documents'] == []
    worker = KnowledgeWorker(env.settings, env.repository, LocalRuntime(),
                             embedding=env.embedding, model=env.model)
    assert worker.run_once(task['id'])
    done = questions.present(questions.require_task(task['id']))
    assert done['status'] == 'succeeded', done
    assert done['result']['status'] == status
    assert done['result']['execution'] == payload['execution']
    assert done['requests_used'] == 0 and done['calls'] == []
    assert done['result']['citations'] == []
    assert questions.submit(conversation['id'], question, 'ask')['id'] == task['id']
    assert not worker.run_once(task['id'])
    assert not env.embedding.calls and not env.model.calls


@pytest.mark.parametrize('question,code', [
    ('你好，早安。', 'sys-welcome'),
    ('您好！晚上好。', 'sys-welcome'),
    ('谢谢，多谢你。', 'sys-thanks'),
    ('你是谁？你能做什么？', 'sys-about'),
    ('您好，说明产品的使用限制。', 'knowledge-default'),
    ('谢谢；文件对归档有什么要求？', 'knowledge-default'),
    ('你好！查询此刻的排班。', 'live-source-unavailable'),
])
def test_compound_pure_interactions_do_not_divert_business_requests(tmp_path, question, code):
    env, _, base = library(tmp_path)
    result = IntentManagement(env.repository).evaluate(question, base['id'])
    assert result['route']['code'] == code
    assert not env.embedding.calls and not env.model.calls
