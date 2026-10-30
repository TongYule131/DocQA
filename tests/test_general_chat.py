"""P1生产编排的隔离工程检查；模型为协议桩，不计语义分类准确率。"""
import json

import pytest
from fastapi import HTTPException

from app.general_chat import POLICY_VERSION, STREAM_VERSION, parse_decision, stream_text
from app.knowledge_questions import KnowledgeQuestions
from app.knowledge_worker import KnowledgeWorker
from app.product_access import ProductAccess
from app.rag_validation import ModelOutputError
from app.unified_chat import UnifiedChat
from tests.test_question_jobs import LocalRuntime, question_fixture
from tests.test_unified_knowledge import context, upload, prepare
from tests.test_product_access import team


def ordinary(tmp_path):
    env = question_fixture(tmp_path, question_max_requests=4)
    access = ProductAccess(env.repository, env.settings, env.rag.index)
    questions = KnowledgeQuestions(access, env.rag)
    chat = UnifiedChat(access, questions)
    return env, questions, chat


def send(chat, questions, *, base=None, ids=None, mode='none', question='P1合成普通问题'):
    conv = chat.create(base, 'create', ids or [], resource_mode=mode)
    submission = chat.submit(conv['id'], question, 'turn', ids or [], resource_mode=mode)
    assert submission['status'] == 'submitted', submission
    return conv, questions.tasks.row(submission['task_id'])


def worker(env):
    return KnowledgeWorker(env.settings, env.repository, LocalRuntime(), embedding=env.embedding, model=env.model)


def test_no_base_general_uses_one_generation_and_no_index_or_embedding(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    env.model.handler = lambda *_: json.dumps({'kind': 'general', 'answer': 'P1合成普通回复'}, ensure_ascii=False)
    conv, task = send(chat, questions)
    assert not env.model.calls and not env.embedding.calls
    assert worker(env).run_once(task['id'])
    detail = chat.detail(conv['id']); turn = detail['turns'][0]
    assert turn['status'] == 'succeeded', turn
    assert turn['result']['response_kind'] == 'general'
    assert turn['result']['status'] == 'answered'
    assert turn['result']['execution']['action'] == 'reply_llm'
    assert turn['result']['citations'] == [] and turn['result']['retrieval']['candidate_count'] == 0
    assert len(env.model.calls) == 1 and not env.embedding.calls
    assert chat.submit(conv['id'], 'P1合成普通问题', 'turn', [], resource_mode='none')['task_id'] == task['id']
    for _ in range(2):
        chat.detail(conv['id']); chat.list(); questions.present(questions.tasks.row(task['id']))
    assert len(env.model.calls) == 1
    with env.repository.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM knowledge_bases').fetchone()[0] == 0
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_selected_preparing_source_does_not_block_ordinary_reply(tmp_path):
    env, access, base, questions, chat = context(tmp_path)
    job = upload(access, base)
    env.model.handler = lambda *_: json.dumps({'kind': 'general', 'answer': '普通回复'})
    conv, task = send(chat, questions, base=base['id'], ids=[job['document_id']], mode='selected')
    worker(env).run_once(task['id'])
    assert chat.detail(conv['id'])['turns'][0]['status'] == 'succeeded'
    assert len(env.model.calls) == 1 and not env.embedding.calls


def test_none_document_decision_requests_material_without_query(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    env.model.handler = lambda *_: json.dumps({'kind': 'document', 'answer': ''})
    conv, task = send(chat, questions)
    worker(env).run_once(task['id'])
    turn = chat.detail(conv['id'])['turns'][0]
    assert turn['status'] == 'succeeded' and turn['result']['status'] == 'clarification_needed'
    assert turn['result']['execution']['state'] == 'needs_materials'
    assert not env.embedding.calls and len(env.model.calls) == 1


def test_invalid_auto_response_is_not_retried_or_fallen_back_to_rag(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    env.model.handler = lambda *_: '{"kind":"general","answer":"合成","citations":[1]}'
    conv, task = send(chat, questions)
    worker(env).run_once(task['id'])
    turn = chat.detail(conv['id'])['turns'][0]
    assert turn['status'] == 'failed' and turn['result'] is None
    assert turn['error_code'] == 'output_general_protocol_invalid'
    assert len(env.model.calls) == 1 and not env.embedding.calls
    assert not worker(env).run_once(task['id'])


def test_resource_mode_is_part_of_idempotent_input(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    conv = chat.create(None, 'new', [], resource_mode='none')
    with pytest.raises(HTTPException):
        chat.create(None, 'new', [], resource_mode='all')
    with pytest.raises(HTTPException):
        chat.submit(conv['id'], 'P1', 'turn', ['qdoc'], resource_mode='none')
    assert not env.model.calls and not env.embedding.calls


@pytest.mark.parametrize('raw', [
    '{"kind":"general","answer":"a","answer":"b"}',
    '{"kind":"document","answer":"猜测文档内容"}',
    '{"kind":"general","answer":""}',
    '{"kind":"tool","answer":""}',
])
def test_strict_decision_rejects_invalid_protocol(raw):
    with pytest.raises(ModelOutputError):
        parse_decision(raw)


def test_partial_stream_decodes_only_general_answer_and_complete_unicode():
    assert stream_text('{"kind":"document","answer":"') == ''
    assert stream_text('{"kind":"general","answer":"a\\n中\\uD83D') == 'a\n中'
    assert stream_text('{"kind":"general","answer":"a\\n中\\uD83D\\uDE00"}') == 'a\n中😀'
    assert parse_decision({'kind': 'general', 'answer': '检查点'})['answer'] == '检查点'


def test_document_decision_reuses_strict_rag_and_freezes_selected_scope(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    access = chat.access; base = access.create_base('P1文档', '')
    access.bind_document(base['id'], 'qdoc'); access.publish(base['id'], 0)
    env.model.handler = lambda system, user: (json.dumps({'kind': 'document', 'answer': ''})
        if system.startswith('你是DocQA助手') else __import__('tests.test_question_jobs', fromlist=['REPLY']).REPLY)
    conv, task = send(chat, questions, base=base['id'], ids=['qdoc'], mode='selected', question='根据文档，全书有多少部分？')
    worker(env).run_once(task['id'])
    turn = chat.detail(conv['id'])['turns'][0]
    assert turn['status'] == 'succeeded', turn
    assert turn['result']['status'] == 'answered' and turn['result'].get('response_kind') != 'general'
    assert {c['document_id'] for c in turn['result']['citations']} == {'qdoc'}
    assert len(env.embedding.calls) == 1 and len(env.model.calls) == 2
    assert turn['requests_used'] == 3


def test_document_waits_without_repeating_paid_dispatch_and_general_still_works(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    access = chat.access; base = access.create_base('P1待准备资料', '')
    job = upload(access, base)
    env.model.handler = lambda system, user: (json.dumps({'kind': 'document', 'answer': ''})
        if system.startswith('你是DocQA助手') else __import__('tests.test_question_jobs', fromlist=['REPLY']).REPLY)
    conv, task = send(chat, questions, base=base['id'], ids=[job['document_id']], mode='selected', question='根据资料，全书有多少部分？')
    runner = worker(env); runner.run_once(task['id'])
    waiting = questions.tasks.row(task['id'])
    assert waiting['status'] == 'queued' and waiting['stage'] == 'waiting_materials', waiting
    assert len(env.model.calls) == 1 and not env.embedding.calls
    assert not runner.run_once(task['id'])
    assert len(env.model.calls) == 1
    prepare(env, access, job)
    before_embedding = len(env.embedding.calls)
    runner.run_once(task['id'])
    turn = chat.detail(conv['id'])['turns'][0]
    assert turn['status'] == 'succeeded', turn
    assert len(env.model.calls) == 2 and len(env.embedding.calls) == before_embedding + 1


def test_cancel_queued_auto_task_sends_nothing(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    conv, task = send(chat, questions)
    from app.conversation_routes import ConversationUpdate
    chat.update(conv['id'],ConversationUpdate(expected_revision=conv['revision'],archived=True))
    assert not worker(env).run_once(task['id'])
    assert not env.model.calls and not env.embedding.calls


def test_general_http_entry_history_export_and_cross_account_denial(team):
    env, users, clients = team
    alice, bob = clients['alice'], clients['bob']
    payload = {'base_id': None, 'document_ids': [], 'resource_mode': 'none', 'request_key': 'general-create'}
    assert clients['anonymous'].post('/api/chat/conversations', json=payload).status_code == 401
    created = alice.post('/api/chat/conversations', json=payload)
    assert created.status_code == 201, created.text
    conv = created.json()
    submitted = alice.post('/api/chat/conversations/' + conv['id'] + '/turns', json={
        'question': 'P1合成普通HTTP问题', 'document_ids': [], 'resource_mode': 'none', 'request_key': 'general-turn'})
    assert submitted.status_code == 202, submitted.text
    task = submitted.json()['task_id']
    env.model.handler = lambda *_: json.dumps({'kind': 'general', 'answer': 'HTTP合成普通回复'})
    worker(env).run_once(task)
    detail = alice.get('/api/chat/conversations/' + conv['id'])
    assert detail.status_code == 200 and detail.json()['turns'][0]['result']['response_kind'] == 'general'
    assert bob.get('/api/chat/conversations/' + conv['id']).status_code == 404
    assert bob.get('/api/kb-tasks/' + task).status_code == 404
    assert alice.get('/api/kb-tasks/' + task + '/export').json()['response_kind'] == 'general'
    assert alice.get('/api/chat/conversations').json()[0]['id'] == conv['id']
    trace=clients['admin'].get('/api/admin/traces/'+task)
    assert trace.status_code==200,trace.text
    assert trace.json()['name']=='general-chat'
    assert trace.json()['query_plan']['name']=='通用LLM回答'
    assert trace.json()['intent']['scope']=='general' and trace.json()['intent']['configuration']
    quota = clients['admin'].get('/api/admin/quality').json()
    assert quota['general_answers'] == 1 and quota['business_tasks']['answered'] == 0
    feedback = alice.post('/api/feedback', json={'source': 'knowledge', 'task_id': task, 'request_key': 'general-feedback',
        'category': 'expression', 'share_context': True})
    assert feedback.status_code == 201, feedback.text
    shared = clients['admin'].get('/api/admin/feedback/' + feedback.json()['id']).json()['shared_context']
    assert shared['reproduction']['chat_policy'] == POLICY_VERSION
    assert shared['reproduction']['conversation_history_shared'] is False
    assert 'history' not in shared['reproduction']
    assert len(env.model.calls) == 1 and not env.embedding.calls


def test_disabled_owner_cannot_start_general_model_call(team):
    env, users, clients = team
    created = clients['alice'].post('/api/chat/conversations', json={
        'base_id': None, 'document_ids': [], 'resource_mode': 'none', 'request_key': 'revoke-create'}).json()
    task = clients['alice'].post('/api/chat/conversations/' + created['id'] + '/turns', json={
        'question': '普通问题', 'document_ids': [], 'resource_mode': 'none', 'request_key': 'revoke-turn'}).json()['task_id']
    assert clients['admin'].patch('/api/admin/users/' + users['alice']['id'], json={'active': False}).status_code == 200
    worker(env).run_once(task)
    with env.repository.connect() as db:
        row = db.execute('SELECT status FROM kb_tasks WHERE id=?', (task,)).fetchone()
    assert row[0] == 'cancelled'
    assert not env.model.calls and not env.embedding.calls


def test_streaming_general_drafts_do_not_count_as_verified_citation_blocks(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    raw = json.dumps({'kind': 'general', 'answer': '普通合成回复。' * 30}, ensure_ascii=False)
    def stream(system, user, on_text, check_active):
        env.model.calls.append((system, user))
        for offset in range(0, len(raw), 13):
            check_active(); on_text(raw[offset:offset + 13])
        return raw
    env.model.generate_stream = stream
    conv, task = send(chat, questions)
    worker(env).run_once(task['id'])
    assert chat.detail(conv['id'])['turns'][0]['status'] == 'succeeded'
    from app.answer_stream_store import AnswerStreamStore
    events = AnswerStreamStore(env.repository).read('knowledge', task['id'])
    blocks = [e['payload'] for e in events if e['kind'] == 'block']
    assert blocks and all(b['version'] == STREAM_VERSION and b['validation_scope'] == 'general_text' for b in blocks)
    from app.operations_metrics import project
    with env.repository.connect() as db:
        assert project(db, '2000-01-01')['first_validated_block']['count'] == 0
    assert len(env.model.calls) == 1 and not env.embedding.calls


def test_timeout_is_uncertain_not_automatic_rag_or_model_retry(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    from app.deepseek import ModelError
    def timeout(*_):
        raise ModelError(504, 'synthetic-timeout')
    env.model.handler = timeout
    conv, task = send(chat, questions)
    runner = worker(env); runner.run_once(task['id'])
    row = questions.tasks.row(task['id'])
    assert row['status'] == 'needs_attention' and row['requests_used'] == 1
    assert not runner.run_once(task['id'])
    assert len(env.model.calls) == 1 and not env.embedding.calls
    with env.repository.connect() as db:
        assert db.execute('SELECT status FROM kb_task_calls WHERE task_id=?', (task['id'],)).fetchone()[0] == 'uncertain'


def test_model_cannot_override_explicit_no_documents_or_real_time_source_guard(tmp_path):
    env, questions, chat = ordinary(tmp_path)
    env.model.handler = lambda *_: json.dumps({'kind': 'document', 'answer': ''})
    conv, task = send(chat, questions, question='不要查知识库，解释一个通用概念')
    worker(env).run_once(task['id'])
    assert questions.tasks.row(task['id'])['status'] == 'failed'
    assert len(env.model.calls) == 1 and not env.embedding.calls
    other = chat.create(None, 'live-create', [], resource_mode='none')
    submitted = chat.submit(other['id'], '查询今天的实时库存数量', 'live-turn', [], resource_mode='none')
    worker(env).run_once(submitted['task_id'])
    result = questions.tasks.row(submitted['task_id'])
    assert result['status'] == 'succeeded'
    assert json.loads(result['result_json'])['execution']['state'] == 'source_unavailable'
    assert len(env.model.calls) == 1 and not env.embedding.calls


@pytest.mark.parametrize('budget', [None, 3, 5])
def test_controlled_tool_preserves_existing_cap_and_counts_parameters_plus_three_http(tmp_path, monkeypatch, budget):
    import httpx
    from app.intent_management import IntentManagement
    from app.knowledge_tools import ToolRegistry
    env, questions, chat = ordinary(tmp_path)
    base = chat.access.create_base('P1合成工具授权', '')
    ToolRegistry(env.repository).save(dict(id='p1-tool', name='P1合成工具', endpoint='https://p1.invalid/mcp',
        tool_name='read_fixture', auth_env=None, input_schema={'type':'object','properties':{'item':{'type':'string'}},
        'required':['item']}, protocol_version='2025-11-25', read_only=True, enabled=True))
    IntentManagement(env.repository).save(dict(code='p1-tool',name='P1合成工具',kind='MCP',base_id=base['id'],
        tool_id='p1-tool',examples=['执行P1合成工具查询'],enabled=True))
    env.model.handler = lambda system, _: (json.dumps({'kind':'controlled','answer':''})
        if system.startswith('你是DocQA助手') else '{"item":"fixture"}')
    conv, task = send(chat, questions, base=base['id'], question='执行P1合成工具查询')
    if budget is not None:
        with env.repository.connect() as db:db.execute('UPDATE kb_tasks SET max_requests=? WHERE id=?',(budget,task['id']))
    sent=[]; client=httpx.Client
    def respond(request):
        body=json.loads(request.content);sent.append(body['method'])
        if body['method']=='notifications/initialized':return httpx.Response(202)
        result={'protocolVersion':'2025-11-25'} if body['method']=='initialize' else {'content':[{'type':'text','text':'合成只读结果'}]}
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})
    monkeypatch.setattr('app.knowledge_tools.httpx.Client',lambda **kw:client(transport=httpx.MockTransport(respond),**kw))
    worker(env).run_once(task['id']);row=questions.tasks.row(task['id'])
    if budget in (None,5):
        assert row['status']=='succeeded',row
        assert row['max_requests']==(8 if budget is None else budget) and row['requests_used']==5
        assert len(env.model.calls)==2 and sent==['initialize','notifications/initialized','tools/call']
        assert chat.detail(conv['id'])['turns'][0]['result']['execution']['state']=='completed'
    else:
        assert row['status']=='failed' and row['error_code']=='budget_exceeded'
        assert row['requests_used']==1 and len(env.model.calls)==1 and not sent
    assert not env.embedding.calls
