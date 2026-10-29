"""礼貌／格式不冒充证据子问；旧协议与流式／落库边界独立验证。"""
import json

import pytest

from app.question_obligations import answer_question_parts,question_obligations
from app.rag_answer_contract import ANSWER_CONTRACT_VERSION,question_parts,validate_task_output
from app.rag_composition import needs_plan,refine_pack,plan_request
from app.rag_prompts import build_task_user_prompt
from app.rag_validation import ModelOutputError
from tests.test_rag_composition import pack


def test_non_evidence_obligations_keep_original_identity_and_text_in_both_prompts():
    question='您好；通行许可何时失效？请保留引用。'
    assert [p['kind'] for p in question_obligations(question)]==['interaction','evidence_question','presentation']
    assert answer_question_parts(question)==[{'part_id':'p2','text':'通行许可何时失效？'}]
    source=refine_pack(pack('通行许可在离开作业区时失效。'))
    user=json.loads(build_task_user_prompt(question=question,document_name='规程',evidence_payload=source.payload()))
    assert user['question']==question and user['question_obligations']==question_obligations(question)
    assert user['question_parts']==json.loads(plan_request(source,question))['question_parts']==answer_question_parts(question)
    assert not needs_plan(question)


@pytest.mark.parametrize('text',[
    '请解释“不要重复”的操作要求。','不要重复执行设备启动步骤。','简短说明校准条件。','请保留原件直到复核结束。',
])
def test_business_conditions_and_quoted_instructions_are_not_removed_as_presentation(text):
    assert all(p['kind']=='evidence_question' for p in question_obligations(text))
    assert answer_question_parts(text)==question_parts(text)


def body(ids):
    return {'status':'answered','conclusion':[{'text':'通行许可在离开作业区时失效。','part_ids':ids,'claim_type':'direct','context':[],
        'evidence':[{'ref':1,'unit':1}]}],'explanation':[],'clarification_questions':[],'missing_points':[],
        'answer_parts':[{'part_id':p,'state':'answered','missing_detail':''} for p in ids],
        'presentation':{'blocks':[{'role':'evidence','kind':'paragraph','fact_indices':[0]}]}}


def test_v9_uses_evidence_parts_and_v8_preserves_its_original_coverage_contract():
    question='许可何时失效？请简短回答。';source=refine_pack(pack('通行许可在离开作业区时失效。'))
    raw=json.dumps(body(['p1']))
    assert validate_task_output(raw,source,question=question,contract_version=ANSWER_CONTRACT_VERSION).status=='answered'
    with pytest.raises(ModelOutputError,match='coverage_mismatch'):
        validate_task_output(raw,source,question=question,contract_version='fact-context-parts-v8')
    historical=json.dumps(body(['p1','p2']))
    assert validate_task_output(historical,source,question=question,contract_version='fact-context-parts-v8').status=='answered'
    with pytest.raises(ModelOutputError,match='coverage_mismatch'):
        validate_task_output(historical,source,question=question,contract_version=ANSWER_CONTRACT_VERSION)


def test_a_fact_cannot_claim_to_answer_a_greeting_or_hide_a_real_second_question():
    source=refine_pack(pack('通行许可在离开作业区时失效。'))
    for question,ids in [('您好；许可何时失效？',['p1']),('许可何时失效？延期条件是什么？',['p1'])]:
        with pytest.raises(ModelOutputError,match='coverage_mismatch'):
            validate_task_output(json.dumps(body(ids)),source,question=question,contract_version=ANSWER_CONTRACT_VERSION)


@pytest.mark.parametrize('scope',['document','knowledge'])
def test_greeting_and_display_do_not_add_calls_and_noncontiguous_part_id_survives_result_recheck(tmp_path,scope):
    from app.answer_stream_store import AnswerStreamStore
    from app.question_worker import QuestionWorker
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    from tests.test_question_jobs import question_fixture,submit,LocalRuntime,REPLY
    from tests.test_knowledge_chain import library
    from tests.test_rag_composition_runtime import kb_provider
    if scope=='document':
        env=question_fixture(tmp_path,question_max_requests=2)
        question='您好；全书分为多少个部分？请保留引用。'
        task,_=submit(env,question=question)
    else:
        env,access,base=library(tmp_path);questions=KnowledgeQuestions(access,env.rag)
        question='您好；普通商品何时可以退货？请保留引用。'
        task=questions.submit(questions.create(base['id'],'obligations')['id'],question,'ask')
    def provider(system,user):
        request=json.loads(user)
        assert request['question_parts']==[{'part_id':'p2','text':question_parts(question)[1]['text']}]
        assert 'evidence_units' not in request
        response=json.loads(REPLY if scope=='document' else kb_provider(system,user))
        for fact in response['conclusion']+response['explanation']:fact['part_ids']=['p2']
        response['answer_parts']=[{'part_id':'p2','state':'answered','missing_detail':''}]
        return json.dumps(response,ensure_ascii=False)
    env.model.handler=provider
    def stream(system,user,on_text,check_active):
        raw=env.model.generate(system,user)
        for start in range(0,len(raw),37):check_active();on_text(raw[start:start+37])
        return raw
    env.model.generate_stream=stream
    if scope=='document':
        QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(task['id'])
        done=env.service.get_job(task['id'])
        assert [p['part_id'] for p in env.service.get_result(done['result_id'])['answer']['answer_parts']]==['p2']
    else:
        KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model).run_once(task['id'])
        done=questions.require_task(task['id'])
        assert [p['part_id'] for p in json.loads(done['result_json'])['answer_parts']]==['p2']
    assert done['status']=='succeeded',done
    assert done['requests_used']==2 and len(env.embedding.calls)==len(env.model.calls)==1
    events=AnswerStreamStore(env.repository).read('question' if scope=='document' else 'knowledge',task['id'])
    assert any(e['kind']=='block' for e in events)
