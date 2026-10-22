"""真实增量传输与局部引用验证，使用合成正文／SDK模拟传输，默认不连供应商。"""
import json
import httpx
import pytest
from openai import OpenAI as RealOpenAI

from app.answer_stream import FactStreamParser
from app.answer_stream_store import AnswerStreamStore
from app.deepseek import DeepSeekModel,ModelError
from app.question_worker import QuestionWorker
from app.rag_validation import ModelOutputError
from tests.test_question_jobs import question_fixture,submit,LocalRuntime,REPLY


def test_incremental_facts_never_expose_partial_strings_or_objects():
    completed=[];parser=FactStreamParser(lambda *args:completed.append(args))
    text='{"conclusion":[{"text":"转义\\\"引号与}括号","evidence":[]}],"explanation":[]}'
    end=text.index('}],')
    for char in text[:end]:parser.feed(char)
    assert not completed
    parser.feed(text[end:])
    assert completed==[('conclusion',0,{'text':'转义"引号与}括号','evidence':[]})]
    with pytest.raises(ModelOutputError):FactStreamParser(lambda *a:None).feed('{"x":1,"x":2}')


def test_document_stream_persists_a_valid_block_before_generation_finishes(tmp_path):
    env=question_fixture(tmp_path);store=AnswerStreamStore(env.repository);early=[]
    class StreamModel:
        def generate(self,*args):raise AssertionError('stream must not fall back to another generation')
        def generate_stream(self,system,user,on_text,check_active):
            for char in REPLY:
                check_active();on_text(char)
                events=store.read('question',job['id'])
                if any(e['kind']=='block' for e in events):
                    with env.repository.connect() as db:
                        row=db.execute('SELECT status,result_id FROM question_jobs WHERE id=?',(job['id'],)).fetchone()
                        assert row['status']=='running' and row['result_id'] is None
                    early.append(True)
            return REPLY
    env.rag.model=StreamModel();job,_=submit(env)
    assert QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    done=env.service.get_job(job['id']);assert done['status']=='succeeded',done
    events=store.read('question',job['id'])
    assert early and [e['kind'] for e in events]==['reset','block']
    block=events[-1]['payload'];assert block['draft'] and block['validation_scope']=='single_fact'
    final=env.service.get_result(done['result_id'])['answer']
    assert block['fact']==final['conclusion'][0]
    assert block['citations']==final['citations']
    assert done['requests_used']==2 and len(env.embedding.calls)==1


def sdk_stream(monkeypatch,settings,*,finish='stop',disconnect=False):
    sent=[];ended=[]
    def chunk(content=None,reason=None,thinking=None):
        delta={}
        if content is not None:delta['content']=content
        if thinking:delta['reasoning_content']=thinking
        return {'id':'synthetic-stream','object':'chat.completion.chunk','created':0,'model':'synthetic',
                'choices':[{'index':0,'delta':delta,'finish_reason':reason}]}
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield ('data: '+json.dumps(chunk(thinking='不要展示的内部思考'))+'\n\n').encode()
            for part in ('{"conclusion":','[]}'):
                yield ('data: '+json.dumps(chunk(content=part))+'\n\n').encode()
            if disconnect:raise httpx.ReadError('synthetic private upstream body')
            ended.append(True)
            yield ('data: '+json.dumps(chunk(reason=finish))+'\n\n').encode()
            yield ('data: '+json.dumps({'id':'synthetic-stream','object':'chat.completion.chunk','created':0,
                'model':'synthetic','choices':[],'usage':{'prompt_tokens':8,'completion_tokens':5,'total_tokens':13}})+'\n\n').encode()
            yield b'data: [DONE]\n\n'
    def handler(request):
        sent.append(json.loads(request.content));return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Body())
    import app.deepseek
    monkeypatch.setattr(app.deepseek,'OpenAI',lambda **kwargs:RealOpenAI(**kwargs,http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    return DeepSeekModel(settings),sent,ended


def test_sdk_real_stream_parameter_and_callbacks_arrive_before_finish(tmp_path,monkeypatch):
    env=question_fixture(tmp_path);model,sent,ended=sdk_stream(monkeypatch,env.settings);pieces=[]
    def text(value):assert not ended;pieces.append(value)
    result=model.generate_stream('合成规则','合成问题',text)
    assert result=='{"conclusion":[]}' and ''.join(pieces)==result
    assert len(sent)==1 and sent[0]['stream'] is True and sent[0]['stream_options']=={'include_usage':True}
    assert '思考' not in result and model.last_call_metadata['usage']['total_tokens']==13
    assert model.last_call_metadata['finish_reason']=='stop'


@pytest.mark.parametrize('finish,disconnect',[('length',False),('stop',True)])
def test_sdk_truncation_and_disconnect_never_retry_or_publish(tmp_path,monkeypatch,finish,disconnect):
    env=question_fixture(tmp_path);model,sent,_=sdk_stream(monkeypatch,env.settings,finish=finish,disconnect=disconnect)
    with pytest.raises(ModelError):model.generate_stream('规则','问题',lambda value:None)
    assert len(sent)==1
