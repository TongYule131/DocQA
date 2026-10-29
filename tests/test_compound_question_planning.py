"""独立多子问规划与费用边界；不用封存候选充当单元测试。"""
import json

import pytest

from app.rag_composition import needs_plan
from app.rag_context import RagError
from app.question_worker import QuestionWorker
from tests.test_question_jobs import question_fixture, submit, LocalRuntime, REPLY, TEXT


QUESTION = '全书分为多少个部分；第一部分是什么？'


@pytest.mark.parametrize('question,expected', [
    ('说明许可的适用对象；解释转交原件的条件。', True),
    ('期限是多少？有什么例外？', True),
    ('普通商品申请条件是什么？请用一句话回答。', False),
    ('说明许可的适用对象；请保留引用。', False),
    ('说明许可的适用对象。', False),
])
def test_multiple_evidence_questions_are_planned_but_display_requests_are_not(question, expected):
    assert needs_plan(question) is expected


def test_new_compound_plan_budget_is_declared_before_any_provider_request(tmp_path):
    env = question_fixture(tmp_path, question_max_requests=2)
    with pytest.raises(RagError):
        submit(env, question=QUESTION)
    assert not env.embedding.calls and not env.model.calls


def test_worker_uses_one_plan_and_one_writer_without_rewriting_or_extra_calls(tmp_path):
    env = question_fixture(tmp_path, question_max_requests=3)
    def provider(system, user):
        value = json.loads(user)
        if 'evidence_units' in value:
            unit = value['evidence_units'][0]
            return json.dumps({'blocks':[{'role':'conclusion','focus':'回答数量和主题',
                'part_ids':['p1','p2'],'groups':[{'label':'本书内容','units':[{'ref':unit['ref'],'unit':unit['unit']}]}]}],
                'missing_part_ids':[]})
        body = json.loads(REPLY)
        body['conclusion'][0]['text'] = TEXT
        body['conclusion'][0]['part_ids'] = ['p1','p2']
        body['answer_parts'] = [dict(part_id=p,state='answered',missing_detail='') for p in ['p1','p2']]
        body['presentation']['blocks'][0]['role'] = 'conclusion'
        return json.dumps(body)
    env.model.handler = provider
    task, _ = submit(env, question=QUESTION)
    assert task['provider_requests_expected'] == 3
    QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(task['id'])
    done = env.service.get_job(task['id'])
    assert done['status'] == 'succeeded', done
    assert done['requests_used'] == 3 and len(env.embedding.calls) == 1 and len(env.model.calls) == 2
