"""会话生成与发布重验使用同一抽取输入；不调用供应商。"""
import copy
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.conversation_memory import MEMORY_SIGNATURE, MEMORY_VERSION, retrieval_question
from app.rag_context import build_evidence
from app.rag_extract_context import SELECTION_VERSION, budget_profile
from app.rag_support_review import restore_admission
from tests.test_retrieval_planning import chunk, vector_result


def admission(memory):
    question = '这份文档讲了什么'
    text = '无关背景。' * 65 + '用户会话上下文必须根据当前问题重新核对。' + '独立材料。' * 85
    assert 700 < len(text) < 900
    chunks = [chunk(0, text)]
    retrieval = vector_result(chunks)
    settings = Settings()
    actual = build_evidence(retrieval, settings, expected_document_id='doc',
        extractive_question=retrieval_question(question, memory), max_items=32, context_max_chars=24000).pack
    snapshot = {'document_id': 'doc', 'parse_version_id': 'v1', 'index_id': 'idx',
        'evidence_selection_version': SELECTION_VERSION,
        'evidence_profile': budget_profile(question, max_items=32, context_chars=24000)}
    if memory is not None:
        snapshot.update(conversation_context=memory, conversation_policy_signature=MEMORY_SIGNATURE)
    selected = [dict(chunk_id=i.chunk_id, reference_id=i.reference_id,
        read_ranges=i.read_ranges, segment_policy=i.segment_policy, unit_choices=i.unit_choices) for i in actual.items]
    repository = SimpleNamespace(chunks=lambda document_id, version: chunks)
    return repository, snapshot, selected, retrieval, question, actual


@pytest.mark.parametrize('memory', [None,
    {'version': MEMORY_VERSION, 'notes': '', 'recent_questions': []},
    {'version': MEMORY_VERSION, 'notes': '核对材料范围', 'recent_questions': ['上下文如何处理？']}])
def test_restore_matches_generation_with_disabled_empty_or_populated_memory(memory):
    repository, snapshot, selected, retrieval, question, actual = admission(memory)
    restored = restore_admission(repository, snapshot, selected, retrieval, question)
    assert restored.budget_profile == actual.budget_profile
    assert [(i.read_ranges, i.unit_choices) for i in restored.items] == [
        (i.read_ranges, i.unit_choices) for i in actual.items]
    if memory:
        assert restored.budget_profile['kind'] != snapshot['evidence_profile']['kind']
        assert restored.items[0].read_ranges != [[0, len(restored.items[0].text)]]


@pytest.mark.parametrize('tamper', ['ranges', 'units', 'memory', 'policy', 'budget'])
def test_restore_still_rejects_modified_ranges_units_memory_and_policy(tamper):
    values = admission({'version': MEMORY_VERSION, 'notes': '', 'recent_questions': []})
    repository, snapshot, selected, retrieval, question, _ = copy.deepcopy(values)
    if tamper == 'ranges': selected[0]['read_ranges'] = [[0, len(values[-1].items[0].text)]]
    if tamper == 'units': selected[0]['unit_choices'] = {}
    if tamper == 'memory': snapshot['conversation_context']['notes'] = 'x' * 801
    if tamper == 'policy': snapshot['conversation_policy_signature'] = 'unknown'
    if tamper == 'budget': snapshot['evidence_profile']['excerpt_chars'] = 123
    with pytest.raises(ValueError):
        restore_admission(repository, snapshot, selected, retrieval, question)
