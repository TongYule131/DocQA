"""普通回答格式与细粒度流式交付；SDK为MockTransport，不访问真实模型。"""
import json
from types import SimpleNamespace
import httpx
import pytest
from openai import OpenAI as RealOpenAI
from app.answer_stream_store import AnswerStreamStore,STREAM_EVENT_LIMIT
from app.deepseek import DeepSeekModel,json_object_output
from app.knowledge_worker import KnowledgeWorker
from tests.test_general_chat import ordinary,send,worker
from tests.test_question_jobs import LocalRuntime,question_fixture
from tests.test_answer_stream import sdk_stream


def advancing_clock(monkeypatch):
    ticks=iter(n/10 for n in range(100000))
    monkeypatch.setattr('app.general_chat.time',SimpleNamespace(perf_counter=lambda:next(ticks)))


def test_first_small_draft_arrives_before_eighty_characters_and_tail_is_not_lost(tmp_path,monkeypatch):
    advancing_clock(monkeypatch)
    env,questions,chat=ordinary(tmp_path);store=AnswerStreamStore(env.repository)
    answer='这是一段普通回答😀。'*5+'尾文'
    raw=json.dumps(dict(kind='general',answer=answer),ensure_ascii=False);first=[]
    conv,task=send(chat,questions)
    def stream(system,user,on_text,check_active):
        env.model.calls.append((system,user))
        for char in raw:
            on_text(char)
            blocks=[e['payload'] for e in store.read('knowledge',task['id']) if e['kind']=='block']
            if blocks and not first:first.append(blocks[0]['text_delta'])
        return raw
    env.model.generate_stream=stream;worker(env).run_once(task['id'])
    row=questions.tasks.row(task['id']);assert row['status']=='succeeded'
    blocks=[e['payload']['text_delta'] for e in store.read('knowledge',task['id']) if e['kind']=='block']
    assert 0<len(first[0])<80 and len(first[0])<=17
    assert ''.join(blocks)==answer and 0<len(blocks[-1])<16
    assert len(env.model.calls)==1 and not env.embedding.calls


@pytest.mark.parametrize('remaining',[0,1,4,1024])
def test_preview_quota_cannot_break_a_valid_answer_or_expand_event_limit(tmp_path,monkeypatch,remaining):
    advancing_clock(monkeypatch)
    env,questions,chat=ordinary(tmp_path);conv,task=send(chat,questions)
    count=STREAM_EVENT_LIMIT-remaining
    with env.repository.connect() as db:
        db.executemany("INSERT INTO answer_stream_events(kb_task_id,attempt_no,kind,payload_json,created_at) VALUES (?,1,'reset','{}','2000-01-01')",[(task['id'],)]*count)
    answer='合成'*8000;raw=json.dumps(dict(kind='general',answer=answer),ensure_ascii=False)
    def stream(system,user,on_text,check_active):
        env.model.calls.append((system,user))
        for offset in range(0,len(raw),97):on_text(raw[offset:offset+97])
        return raw
    env.model.generate_stream=stream;worker(env).run_once(task['id'])
    assert questions.tasks.row(task['id'])['status']=='succeeded'
    with env.repository.connect() as db:
        assert db.execute('SELECT count(*) FROM answer_stream_events WHERE kb_task_id=?',(task['id'],)).fetchone()[0]<=STREAM_EVENT_LIMIT
    assert len(env.model.calls)==1 and not env.embedding.calls


def test_actual_dispatch_sdk_requests_json_once_and_keeps_malformed_first_response(tmp_path,monkeypatch):
    env,questions,chat=ordinary(tmp_path)
    raw='{"kind":"general","answer":"这是没有闭合的合成回答。'
    model,sent,_=sdk_stream(monkeypatch,env.settings,text_parts=[raw])
    conv,task=send(chat,questions)
    runner=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=model)
    runner.run_once(task['id']);row=questions.tasks.row(task['id'])
    assert row['status']=='failed' and not row['result_json'] and row['requests_used']==1
    assert len(sent)==1 and sent[0]['response_format']=={'type':'json_object'}
    with env.repository.connect() as db:
        saved=json.loads(db.execute("SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name='diagnostic_validation_attempt_1'",(task['id'],)).fetchone()[0])
    assert saved['output']==raw and saved['reason']=='general_protocol_invalid'
    assert not runner.run_once(task['id']) and len(sent)==1 and not env.embedding.calls


def test_json_output_scope_does_not_leak_to_subsequent_streams(tmp_path,monkeypatch):
    env=question_fixture(tmp_path);model,sent,_=sdk_stream(monkeypatch,env.settings)
    with pytest.raises(RuntimeError):
        with json_object_output():
            model.generate_stream('JSON规则','合成输入',lambda _:None)
            raise RuntimeError('fixture')
    model.generate_stream('另一阶段','合成输入',lambda _:None)
    assert len(sent)==2 and sent[0]['response_format']=={'type':'json_object'} and 'response_format' not in sent[1]


def test_json_output_scope_also_applies_to_nonstream_dispatch_only(tmp_path,monkeypatch):
    env=question_fixture(tmp_path);sent=[]
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':'fixture',
            'choices':[{'index':0,'message':{'role':'assistant','content':'{"kind":"general","answer":"合成"}'},'finish_reason':'stop'}]})
    monkeypatch.setattr('app.deepseek.OpenAI',lambda **kw:RealOpenAI(**kw,http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    model=DeepSeekModel(env.settings)
    with json_object_output():model.generate('JSON规则','输入')
    model.generate('另一阶段','输入')
    assert sent[0]['response_format']=={'type':'json_object'} and 'response_format' not in sent[1] and len(sent)==2
