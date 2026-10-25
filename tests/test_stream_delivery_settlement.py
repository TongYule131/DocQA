"""一个HTTP中的本地守卫首错可完整结算；用户取消仍立即中止。"""
import json
import pytest
from app.rag_validation import ModelOutputError
from app.knowledge_tasks import TaskStopped
from tests.test_question_jobs import question_fixture
from tests.test_answer_stream import sdk_stream


def test_local_validation_error_drains_same_http_without_showing_more_blocks(tmp_path,monkeypatch):
    env=question_fixture(tmp_path);model,sent,ended=sdk_stream(monkeypatch,env.settings);callbacks=[]
    def reject(text):callbacks.append(text);raise ModelOutputError('incomplete_quote_sentence')
    with pytest.raises(ModelOutputError,match='incomplete_quote_sentence') as error:model.generate_stream('规则','问题',reject)
    assert len(sent)==1 and ended and len(callbacks)==1
    assert error.value.raw_output=='{"conclusion":[]}'
    assert model.last_call_metadata['usage']['total_tokens']==13
    with env.repository.connect() as db:
        call=db.execute('SELECT status,delivery_status,usage_json FROM provider_guard_calls').fetchone()
        assert tuple(call)[:2]==('failed','sent') and json.loads(call['usage_json'])['total_tokens']==13
        assert db.execute('SELECT failures FROM provider_circuits').fetchone()[0]==0


def test_user_cancel_still_stops_stream_and_records_uncertainty(tmp_path,monkeypatch):
    env=question_fixture(tmp_path);model,sent,ended=sdk_stream(monkeypatch,env.settings)
    def cancel(text):raise TaskStopped('cancelled','停止','cancelled')
    with pytest.raises(TaskStopped) as error:model.generate_stream('规则','问题',cancel)
    assert len(sent)==1 and not ended and error.value.uncertain
    with env.repository.connect() as db:
        assert db.execute('SELECT status FROM provider_guard_calls').fetchone()[0]=='uncertain'
        assert db.execute('SELECT failures FROM provider_circuits').fetchone()[0]==0
