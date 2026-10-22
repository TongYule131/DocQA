"""流式事件的持久化、重放、失败、恢复及逐对象授权。"""
import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from app.answer_stream_store import AnswerStreamStore
from app.deepseek import ModelError
from app.main import create_app
from app.question_worker import QuestionWorker
from tests.test_question_jobs import question_fixture,submit,LocalRuntime,REPLY,QUESTION
from tests.test_product_access import team
from tests.test_conversations import conversation,turn


class Streaming:
    def __init__(self,reply=REPLY,after=None):self.reply=reply;self.after=after;self.calls=0
    def generate(self,*args):raise AssertionError('never fall back')
    def generate_stream(self,system,user,on_text,check_active):
        self.calls+=1
        for text in self.reply:
            check_active();on_text(text)
            if self.after:self.after(check_active)
        return self.reply


def run(env,job):
    return QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])


def test_sse_replay_and_cursor_are_read_only_and_do_not_duplicate_generation(tmp_path):
    env=question_fixture(tmp_path);model=Streaming();env.rag.model=model;job,_=submit(env)
    run(env,job)
    rows=AnswerStreamStore(env.repository).read('question',job['id']);assert len(rows)==2
    path='/api/question-jobs/'+job['id']+'/events'
    with TestClient(create_app(env.settings)) as client:
        first=client.get(path);assert first.status_code==200,first.text
        assert first.headers['content-type'].startswith('text/event-stream')
        assert first.text.count('event: update')==2 and first.text.count('event: state')==1
        rest=client.get(path,headers={'Last-Event-ID':str(rows[0]['id'])})
        assert rest.text.count('event: update')==1
        end=client.get(path,params={'after':rows[-1]['id']})
        assert 'event: update' not in end.text and '"status":"succeeded"' in end.text
        assert client.get(path,headers={'Last-Event-ID':'invalid'}).status_code==422
        assert client.get(path,params={'after':-1}).status_code==422
        assert client.get('/api/question-jobs/missing/events').status_code==404
    assert model.calls==1 and len(env.embedding.calls)==1 and env.service.get_job(job['id'])['requests_used']==2


@pytest.mark.parametrize('failure',['invalid_unit','invalid_final','truncated','disconnect'])
def test_bad_or_interrupted_stream_never_becomes_a_final_answer(tmp_path,failure):
    env=question_fixture(tmp_path);raw=REPLY
    if failure=='invalid_unit':
        payload=json.loads(raw);payload['conclusion'][0]['evidence'][0]['unit']=999;raw=json.dumps(payload,ensure_ascii=False)
    if failure=='invalid_final':raw=raw[:-1]+',"unknown":"must reject"}'
    class Failing(Streaming):
        def generate_stream(self,*args):
            value=super().generate_stream(*args)
            if failure in ('truncated','disconnect'):raise ModelError(502 if failure=='truncated' else 504,'synthetic interruption')
            return value
    model=Failing(raw);env.rag.model=model;job,_=submit(env);run(env,job)
    done=env.service.get_job(job['id']);assert done['status'] in ('failed','needs_attention') and not done['result_id']
    blocks=[e for e in AnswerStreamStore(env.repository).read('question',job['id']) if e['kind']=='block']
    assert len(blocks)==(0 if failure=='invalid_unit' else 1)
    assert not run(env,job) and model.calls==1 and done['requests_used']==2


def test_cancel_after_first_block_stops_further_output_and_does_not_save_result(tmp_path):
    env=question_fixture(tmp_path);job,_=submit(env);store=AnswerStreamStore(env.repository)
    def after(check):
        if any(e['kind']=='block' for e in store.read('question',job['id'])):
            with env.repository.connect() as db:db.execute('UPDATE question_jobs SET cancel_requested=1 WHERE id=?',(job['id'],))
            check()
    env.rag.model=Streaming(after=after);run(env,job)
    done=env.service.get_job(job['id']);assert done['status']=='cancelled' and done['result_id'] is None
    assert len(store.read('question',job['id']))==2 and env.rag.model.calls==1


def test_old_nonstream_snapshot_keeps_compatible_execution(tmp_path):
    env=question_fixture(tmp_path);job,_=submit(env)
    with env.repository.connect() as db:
        raw=json.loads(db.execute('SELECT snapshot_json FROM question_jobs WHERE id=?',(job['id'],)).fetchone()[0])
        raw.pop('stream_protocol');db.execute('UPDATE question_jobs SET snapshot_json=? WHERE id=?',(json.dumps(raw),job['id']))
    run(env,job);assert env.service.get_job(job['id'])['status']=='succeeded'
    assert not AnswerStreamStore(env.repository).read('question',job['id'])


def test_private_stream_is_not_readable_by_admin_or_other_customer(team):
    env,_,clients,_,conv=conversation(team);env.rag.model=Streaming()
    job=turn(clients['alice'],conv['id'],question=QUESTION).json()['job'];run(env,job)
    path='/api/question-jobs/'+job['id']+'/events'
    assert clients['alice'].get(path).status_code==200
    assert clients['admin'].get(path).status_code==404
    assert clients['bob'].get(path).status_code==404


@pytest.mark.parametrize('revoke',['membership','session','disabled'])
def test_permission_is_rechecked_between_replayed_blocks(team,revoke):
    env,_,clients,base,conv=conversation(team);env.rag.model=Streaming()
    job=turn(clients['alice'],conv['id'],question=QUESTION).json()['job'];run(env,job)
    app=create_app(env.settings);endpoint=next(r.endpoint for r in app.routes if getattr(r,'path','')=='/api/question-jobs/{job_id}/events')
    class Request:
        headers={};cookies={'docqa_session':clients['alice'].cookies.get('docqa_session')}
        async def is_disconnected(self):return False
    async def scenario():
        response=await endpoint(job['id'],Request(),0);iterator=response.body_iterator
        first=await anext(iterator);assert 'event: update' in first
        with env.repository.connect() as db:
            if revoke=='membership':db.execute('DELETE FROM knowledge_base_members WHERE knowledge_base_id=?',(base['id'],))
            elif revoke=='session':db.execute('UPDATE product_sessions SET expires_at=0')
            else:db.execute("UPDATE product_users SET active=0 WHERE username='alice'")
        second=await anext(iterator);assert 'event: denied' in second
        assert 'citations' not in second
        await iterator.aclose()
    asyncio.run(scenario())


def test_kb_chat_streams_from_frozen_authorized_library(tmp_path):
    from tests.test_knowledge_chain import library
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    env,access,base=library(tmp_path);service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'create')
    class LibraryStream(Streaming):
        def generate_stream(self,system,user,on_text,check_active):
            ref=next(r['ref'] for r in json.loads(user)['references'] if r['document_name']=='合成问答样本.txt')
            payload=json.loads(REPLY);payload['conclusion'][0]['evidence'][0]['ref']=ref;payload['conclusion'][0]['context'][0]['ref']=ref
            self.reply=json.dumps(payload,ensure_ascii=False)
            return super().generate_stream(system,user,on_text,check_active)
    model=LibraryStream();task=service.submit(conv['id'],QUESTION,'turn')
    assert KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=model).run_once(task['id'])
    done=service.present(service.require_task(task['id']));assert done['status']=='succeeded',done
    events=AnswerStreamStore(env.repository).read('knowledge',task['id'])
    assert len(events)==2 and events[-1]['payload']['citations'][0]['filename']=='合成问答样本.txt'
    with TestClient(create_app(env.settings)) as client:
        response=client.get('/api/kb-tasks/'+task['id']+'/events');assert response.status_code==200
        assert 'event: update' in response.text
    assert model.calls==1 and len(env.embedding.calls)==1 and done['requests_used']==2


def test_worker_crash_with_inflight_stream_is_not_automatically_regenerated(tmp_path):
    env=question_fixture(tmp_path);job,_=submit(env);store=AnswerStreamStore(env.repository)
    def crash(check):
        if any(e['kind']=='block' for e in store.read('question',job['id'])):raise KeyboardInterrupt('synthetic worker exit')
    env.rag.model=Streaming(after=crash)
    with pytest.raises(KeyboardInterrupt):run(env,job)
    with env.repository.connect() as db:db.execute("UPDATE question_jobs SET lease_expires_at='2000-01-01' WHERE id=?",(job['id'],))
    assert not run(env,job)
    done=env.service.get_job(job['id']);assert done['status']=='needs_attention' and not done['result_id']
    assert env.rag.model.calls==1 and len(store.read('question',job['id']))==2


def test_unknown_stream_protocol_is_rejected_before_provider_calls(tmp_path):
    env=question_fixture(tmp_path);env.rag.model=Streaming();job,_=submit(env)
    with env.repository.connect() as db:
        payload=json.loads(db.execute('SELECT snapshot_json FROM question_jobs WHERE id=?',(job['id'],)).fetchone()[0])
        payload['stream_protocol']='unknown';db.execute('UPDATE question_jobs SET snapshot_json=? WHERE id=?',(json.dumps(payload),job['id']))
    run(env,job);assert env.service.get_job(job['id'])['status']=='failed'
    assert not env.embedding.calls and env.rag.model.calls==0


def test_v13_stream_migration_failure_rolls_back_without_changing_old_data(tmp_path,monkeypatch):
    from app import migrations
    from app.repository import Repository
    repository=Repository(tmp_path/'docqa.db')
    with monkeypatch.context() as old:
        old.setattr(migrations,'SCHEMA_VERSION',12);repository.initialize()
    with repository.connect() as db:before=[tuple(r) for r in db.execute('SELECT * FROM intent_nodes ORDER BY id')]
    def fail():raise RuntimeError('synthetic stream migration failure')
    migrations.MigrationHook.register(13,fail)
    try:
        with pytest.raises(migrations.MigrationError):repository.initialize()
    finally:migrations.MigrationHook.clear()
    with repository.connect() as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==12
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='answer_stream_events'").fetchone()
        assert [tuple(r) for r in db.execute('SELECT * FROM intent_nodes ORDER BY id')]==before
    assert repository.initialize()['applied']==list(range(13,migrations.SCHEMA_VERSION+1))
    assert repository.initialize()['applied']==[]
