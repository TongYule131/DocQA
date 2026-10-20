"""在线复验前先生成可审阅计划，准备计划本身无收费能力。"""
import json
import runpy
from pathlib import Path

import pytest

from app.rag_evaluation import EvaluationError
from scripts.prepare_rag_live_plan import prepare


def test_plan_has_no_approval_or_calls_and_preserves_three_branches(tmp_path):
    cases=runpy.run_path(str(Path(__file__).parent/'fixtures/rag_quality/golden.py'))['make_cases']()[:2]
    target=tmp_path/'planned'
    result=prepare(cases,target)
    assert result['approval_status']=='not_approved' and result['provider_calls_this_run']==0
    assert result['planned_requests']=={'index':0,'query':2,'generation':6,'total':8}
    assert len(list(target.glob('request-preview-*.json')))==6
    assert not (target/'approval.json').exists() and not (target/'request-ledger.db').exists()
    for file in target.glob('request-preview-*.json'):
        preview=json.loads(file.read_text(encoding='utf-8'))
        assert preview['preview_only'] and preview['input_chars']<=20000
    with pytest.raises(EvaluationError,match='plan_directory_exists'):
        prepare(cases,target)


def test_complete_evidence_cannot_be_silently_trimmed_to_fit(tmp_path):
    case=runpy.run_path(str(Path(__file__).parent/'fixtures/rag_quality/golden.py'))['make_cases']()[0]
    case['context_max_chars']=1
    with pytest.raises(EvaluationError,match='complete_evidence_exceeds_same_budget'):
        prepare([case],tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_preview_must_not_trust_chunk_id_when_text_was_replaced(tmp_path):
    case=runpy.run_path(str(Path(__file__).parent/'fixtures/rag_quality/golden.py'))['make_cases']()[0]
    case['vector_result']['results'][0]['text']='编号相同但正文被替换。'
    with pytest.raises(EvaluationError,match='preview_text_or_scope_changed'):
        prepare([case],tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
