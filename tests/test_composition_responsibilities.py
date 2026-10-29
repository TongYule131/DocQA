"""段落职责、重复事实和全篇出口；不改题或用新标注调参。"""
from copy import deepcopy
import json

import pytest

from app.rag_answer_contract import ANSWER_CONTRACT_VERSION, validate_task_output
from app.rag_composition import refine_pack, validate_writer_plan, writer_prompt
from app.rag_prompts import build_task_user_prompt
from app.rag_validation import ModelOutputError
from tests.test_rag_composition import pack


QUESTION = '说明甲方案的适用条件；解释乙方案的维护责任。'


def inputs():
    source = refine_pack(pack('甲方案仅供内部使用。乙方案由使用方维护。'))
    plan = {'blocks': [
        {'role': 'conclusion', 'focus': '甲的适用条件', 'part_ids': ['p1'],
         'groups': [{'label': '甲', 'units': [{'ref': 1, 'unit': 1}]}]},
        {'role': 'mechanism', 'focus': '乙的维护责任', 'part_ids': ['p2'],
         'groups': [{'label': '乙', 'units': [{'ref': 1, 'unit': 2}]}]},
    ], 'missing_part_ids': []}
    def fact(text, part):
        return {'text': text, 'part_ids': [part], 'claim_type': 'direct', 'context': [],
                'evidence': [{'ref': 1, 'unit': 1}, {'ref': 1, 'unit': 2}]}
    body = {'status': 'answered', 'conclusion': [fact('甲方案仅供内部使用。', 'p1')],
            'explanation': [fact('乙方案由使用方维护。', 'p2')],
            'clarification_questions': [], 'missing_points': [],
            'answer_parts': [dict(part_id=p, state='answered', missing_detail='') for p in ['p1', 'p2']],
            'presentation': {'blocks': [
                dict(role=role, kind='paragraph', fact_indices=[i])
                for i, role in enumerate(['conclusion', 'mechanism'])]}}
    return source, plan, body


def test_globally_covered_subquestions_cannot_silently_swap_paragraph_responsibilities():
    source, plan, body = inputs()
    body['conclusion'][0]['part_ids'] = ['p2']
    body['explanation'][0]['part_ids'] = ['p1']
    raw = json.dumps(body)
    # 全篇声明齐全仍不等于执行了段落计划；引用与数字校验独立保留。
    validate_task_output(raw, source, question=QUESTION, contract_version=ANSWER_CONTRACT_VERSION)
    with pytest.raises(ModelOutputError, match='composition_coverage_invalid'):
        validate_writer_plan(raw, plan, source, question=QUESTION)


def test_repeated_identical_fact_cannot_impersonate_progress_by_changing_part_ids():
    source, plan, body = inputs()
    body['explanation'][0] = deepcopy(body['conclusion'][0])
    body['explanation'][0]['part_ids'] = ['p2']
    with pytest.raises(ModelOutputError, match='answer_redundant_fact'):
        validate_writer_plan(json.dumps(body), plan, source, question=QUESTION)


def test_joint_opening_can_cover_later_part_but_later_paragraph_must_still_do_its_job():
    source, plan, body = inputs()
    body['conclusion'][0]['part_ids'].append('p2')
    validate_writer_plan(json.dumps(body), plan, source, question=QUESTION)
    body['explanation'][0]['part_ids'] = ['p1']
    with pytest.raises(ModelOutputError, match='composition_coverage_invalid'):
        validate_writer_plan(json.dumps(body), plan, source, question=QUESTION)


def test_identical_wording_with_different_explicit_conditions_is_not_deleted_or_rejected():
    source, plan, body = inputs()
    body['explanation'][0]['text'] = body['conclusion'][0]['text']
    body['explanation'][0]['context'] = [dict(kind='subject', ref=1, span=1, segment=2)]
    validate_writer_plan(json.dumps(body), plan, source, question=QUESTION)


def test_writer_receives_local_subquestion_obligations_and_explicit_gaps():
    source, plan, _ = inputs()
    user = build_task_user_prompt(question=QUESTION, document_name='合成规则', evidence_payload=source.payload())
    request = json.loads(writer_prompt(user, source, plan))
    assert request['composition_part_obligations'] == [
        {'block_index': 0, 'planned_part_ids': ['p1']},
        {'block_index': 1, 'planned_part_ids': ['p2']}]
    assert request['composition_missing_part_ids'] == []


def test_shared_paragraph_responsibility_does_not_require_repeating_every_subquestion():
    source, plan, body = inputs()
    plan['blocks'][1]['part_ids'] = ['p1', 'p2']
    validate_writer_plan(json.dumps(body), plan, source, question=QUESTION)
    validate_task_output(json.dumps(body), source, question=QUESTION, contract_version=ANSWER_CONTRACT_VERSION)


def test_reordering_evidence_cannot_hide_an_identical_repeated_fact():
    source, plan, body = inputs()
    body['explanation'][0] = deepcopy(body['conclusion'][0])
    body['explanation'][0]['part_ids'] = ['p2']
    body['explanation'][0]['evidence'].reverse()
    with pytest.raises(ModelOutputError, match='answer_redundant_fact'):
        validate_writer_plan(json.dumps(body), plan, source, question=QUESTION)


@pytest.mark.parametrize('baseline',[False,True])
def test_generic_three_stage_comparison_allows_bounded_layouts_without_importing_a_different_domain(baseline):
    from app.rag_composition import organization_brief,validate_plan
    question='比较三个阶段的设备检查职责。'
    source=refine_pack(pack('阶段甲核对设备身份。阶段乙检查密封状态。阶段丙复核异常记录。'))
    groups=[{'label':label,'label_source':{'ref':1,'unit':i},'units':[{'ref':1,'unit':i}]}
            for i,label in enumerate(['阶段甲','阶段乙','阶段丙'],1)]
    def block(role,values):return {'role':role,'focus':'比较职责变化','part_ids':['p1'],'groups':values}
    blocks=([block('procedure',groups[:1])] if baseline else [])+[
        block('comparison',groups[:2]),block('comparison',groups[1:])]
    value={'blocks':blocks,'missing_part_ids':[]}
    validate_plan(value,source,question)
    brief=organization_brief(question)
    assert '网络' not in brief['comparison_obligation'] and '数据驱动' not in brief['stage_coverage']
    value['blocks'][-1]['groups'][0]['label_source']={'ref':1,'unit':3}
    with pytest.raises(ModelOutputError,match='composition_binding_invalid'):
        validate_plan(value,source,question)


def test_whole_answer_rejects_duplicates_without_a_composition_plan_but_keeps_historical_rendering():
    from app.rag_presentation import render_answer
    source, _, body = inputs()
    body['explanation'][0] = deepcopy(body['conclusion'][0])
    body['explanation'][0]['part_ids'] = ['p2']
    raw = json.dumps(body)
    with pytest.raises(ModelOutputError, match='answer_redundant_fact'):
        validate_task_output(raw, source, question=QUESTION, contract_version=ANSWER_CONTRACT_VERSION)
    # 局部校验不宣称全篇成功；历史已保存结构仍可按原样显示，不批量清洗。
    old = validate_task_output(raw, source, question=QUESTION, contract_version=ANSWER_CONTRACT_VERSION, _whole_answer=False)
    assert render_answer(old).count('甲方案仅供内部使用。') == 2


@pytest.mark.parametrize('scope', ['document', 'knowledge'])
@pytest.mark.parametrize('planned', [True, False])
def test_locally_valid_stream_drafts_cannot_publish_a_redundant_whole_answer(tmp_path, scope, planned):
    from app.answer_stream_store import AnswerStreamStore
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    from app.question_worker import QuestionWorker
    from tests.test_knowledge_chain import library
    from tests.test_question_jobs import question_fixture, submit, LocalRuntime, QUESTION as simple_question
    from tests.test_rag_composition_runtime import QUESTION as comparison_question, provider, kb_provider

    question = comparison_question if planned else simple_question
    if scope == 'document':
        env = question_fixture(tmp_path, question_max_requests=3)
        task, _ = submit(env, question=question)
        reply = provider
    else:
        env, access, base = library(tmp_path)
        questions = KnowledgeQuestions(access, env.rag)
        task = questions.submit(questions.create(base['id'], 'stream-responsibility')['id'], question, 'ask')
        reply = kb_provider
    store = AnswerStreamStore(env.repository)
    early = []
    def generate_stream(system, user, on_text, check_active):
        env.model.calls.append((system, user))
        body = json.loads(reply(system, user))
        body['explanation'] = [deepcopy(body['conclusion'][0])]
        body['presentation']['blocks'][0]['fact_indices'] = [0, 1]
        raw = json.dumps(body, ensure_ascii=False)
        for offset in range(0, len(raw), 17):
            check_active()
            on_text(raw[offset:offset + 17])
            blocks = [e for e in store.read('question' if scope == 'document' else 'knowledge', task['id']) if e['kind'] == 'block']
            if blocks:
                early.extend(blocks)
        return raw
    env.model.handler = reply
    env.model.generate_stream = generate_stream
    if scope == 'document':
        assert QuestionWorker(env.settings, env.repository, rag=env.rag, runtime=LocalRuntime()).run_job(task['id'])
        done = env.service.get_job(task['id'])
        assert done['result_id'] is None
    else:
        worker = KnowledgeWorker(env.settings, env.repository, LocalRuntime(), embedding=env.embedding, model=env.model)
        assert worker.run_once(task['id'])
        done = questions.require_task(task['id'])
        assert done['result_json'] is None
    assert done['status'] == 'failed' and done['requests_used'] == 2 + int(planned), done
    assert early and all(e['payload']['draft'] and e['payload']['validation_scope'] == 'single_fact' for e in early)
    assert len(env.embedding.calls) == 1 and len(env.model.calls) == 1 + int(planned)
