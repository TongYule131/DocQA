"""独立收尾入口的授权/输入守卫：全部离线，不访问历史账本或真实配置。"""
import json
from hashlib import sha256

import pytest

from scripts import evaluate_stable_closure as closure


def frozen_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(closure, 'PHASE_DIR', tmp_path)
    sample = tmp_path / 'sample.txt'
    sample.write_text('合成样本', encoding='utf-8')
    plan = {'phase_id': closure.PHASE_ID, 'planned_requests': 8,
            'cases': [{'case_id': c, 'filename': c + '.txt', 'executable': True,
                       'requests': 1, 'batches': 1} for c in ('E1', 'E2', 'E3', 'E4', 'S1', 'S2', 'S3', 'S4')],
            'files': [{'path': str(sample), 'sha256': sha256(sample.read_bytes()).hexdigest()}]}
    (tmp_path / 'frozen-plan.json').write_text(json.dumps(plan), encoding='utf-8')
    monkeypatch.setattr(closure, 'run_online', lambda *_: pytest.fail('守卫之前不应读取配置或发请求'))
    return sample


def test_default_checks_plan_without_authorization_or_model(tmp_path, monkeypatch):
    frozen_phase(tmp_path, monkeypatch)
    assert closure.main([]) == 0
    assert not (tmp_path / 'online-ledger.json').exists()


def test_offline_status_reports_existing_cost_without_reset(tmp_path, monkeypatch, capsys):
    frozen_phase(tmp_path, monkeypatch)
    ledger_path = tmp_path / 'online-ledger.json'
    ledger_path.write_text(json.dumps({'used': 1, 'total_budget': 13}), encoding='utf-8')
    before = ledger_path.read_bytes()
    assert closure.main([]) == 0
    assert json.loads(capsys.readouterr().out)['paid_requests'] == 1
    assert ledger_path.read_bytes() == before


def test_online_without_separate_phase_approval_stops_before_config(tmp_path, monkeypatch):
    frozen_phase(tmp_path, monkeypatch)
    assert closure.main(['--allow-online']) == 2
    assert not (tmp_path / 'online-ledger.json').exists()


def test_changed_input_stops_before_model_or_ledger(tmp_path, monkeypatch):
    sample = frozen_phase(tmp_path, monkeypatch)
    sample.write_text('输入被改变', encoding='utf-8')
    assert closure.main(['--allow-online']) == 2
    assert not (tmp_path / 'online-ledger.json').exists()


@pytest.mark.parametrize('cap', [True, 7, 17])
def test_invalid_or_excessive_authorized_budget_is_rejected(tmp_path, monkeypatch, cap):
    frozen_phase(tmp_path, monkeypatch)
    plan, plan_hash = closure.verify_plan()
    (tmp_path / 'approval.json').write_text(json.dumps({
        'approved': True, 'phase_id': closure.PHASE_ID, 'plan_sha256': plan_hash,
        'max_requests': cap, 'authorization_source': '合成守卫测试，不构成真实授权'}), encoding='utf-8')
    assert closure.main(['--allow-online']) == 2


def production_phase(tmp_path, monkeypatch, *, fail=False):
    """真实任务/账本编排，仅替换生成模型；测试授权记录不允许触达任何真实端点。"""
    from app.analysis_jobs import AnalysisJobService, analysis_model_signature
    from app.analysis_worker import _units_with_local_ids
    from app.config import Settings
    from app.deepseek import ModelError
    from app.extraction import build_batch_request as extract_request
    from app.summarization import build_batch_request as summary_request
    from app.repository import Repository
    from tests.test_analysis import fixture_settings, seed_document, extraction_reply, summary_reply

    monkeypatch.setattr(closure, 'PHASE_DIR', tmp_path)
    monkeypatch.setenv('DOCQA_DATA_DIR', str(tmp_path / 'case-data'))
    settings = fixture_settings(tmp_path / 'case-data', deepseek_api_key='offline-only',
                                analysis_batch_max_chars=12000)
    monkeypatch.setattr(Settings, 'from_env', classmethod(lambda cls: settings))
    repository = Repository(settings.data_dir / 'docqa.db')
    seed_document(repository)
    service = AnalysisJobService(settings, repository)
    cases, calls = [], []
    for case_id in ('E1', 'E2', 'E3', 'E4', 'S1', 'S2', 'S3', 'S4'):
        kind = 'extraction' if case_id.startswith('E') else 'summary'
        outcome = service.build_plan('doc-1', kind)
        builder = extract_request if kind == 'extraction' else summary_request
        request = builder(units=_units_with_local_ids(outcome.plan.batches[0]),
                          document_name=outcome.document.filename, version_id=outcome.version_id,
                          batch_index=1, batch_total=1)
        request_hash = sha256((request.system_prompt + '\0' + request.user_prompt).encode()).hexdigest()
        cases.append({'case_id': case_id, 'document_id': 'doc-1', 'kind': kind,
                      'parse_version_id': outcome.version_id, 'plan_fingerprint': outcome.plan.fingerprint,
                      'requests': 1, 'batch_request_hashes': [request_hash], 'reduce_required': False})

    class FakeModel:
        def __init__(self, _settings):
            pass
        def generate(self, system, user):
            calls.append(user)
            if fail:
                raise ModelError(502, '合成模型故障')
            payload = json.loads(user)
            unit = next(u for u in payload['units'] if '营业收入为' in u['content'])
            quote, ref = unit['content'], unit['ref']
            if '提取' in payload['task']:
                return extraction_reply([{'kind': 'data', 'content': quote, 'refs': [ref]}], {str(ref): quote})
            return summary_reply('合成说明', [{'text': quote, 'refs': [ref]}], [], {str(ref): quote})

    monkeypatch.setattr('app.deepseek.DeepSeekModel', FakeModel)
    plan = {'cases': cases, 'analysis_settings': {}, 'model_signature': analysis_model_signature(settings)}
    return plan, calls


def test_production_phase_budget_and_completed_jobs_survive_rerun(tmp_path, monkeypatch):
    plan, calls = production_phase(tmp_path, monkeypatch)
    assert closure.run_online(plan, 8) == 0
    assert len(calls) == 8
    ledger = json.loads((tmp_path / 'online-ledger.json').read_text(encoding='utf-8'))
    assert ledger['used'] == 8 and len(ledger['entries']) == 8
    assert ledger['online_started'] is True
    assert closure.run_online(plan, 8) == 0
    assert len(calls) == 8
    assert json.loads((tmp_path / 'online/E1-result.json').read_text(encoding='utf-8'))['semantic_verdict'] == 'pending_manual_review'


def test_failed_attempt_stays_charged_and_script_does_not_resend(tmp_path, monkeypatch):
    plan, calls = production_phase(tmp_path, monkeypatch, fail=True)
    assert closure.run_online(plan, 8) == 1
    assert len(calls) == 1
    ledger = json.loads((tmp_path / 'online-ledger.json').read_text(encoding='utf-8'))
    assert ledger['used'] == 1 and ledger['entries'][0]['status'] == 'uncertain'
    with pytest.raises(ValueError, match='自动重试'):
        closure.run_online(plan, 8)
    assert len(calls) == 1


@pytest.mark.parametrize('finish_reason', ['stop', 'length'])
def test_http_evidence_preserves_final_body_before_sdk_validation(tmp_path, monkeypatch, finish_reason):
    import httpx
    from app.config import Settings
    from app.deepseek import ModelError

    requests = []
    body = '  合成最终正文  '

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            'choices': [{'index': 0, 'finish_reason': finish_reason, 'message': {
                'role': 'assistant', 'content': body, 'reasoning_content': '不得保存的思考内容'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30},
        })

    monkeypatch.setattr('openai.DefaultHttpxClient', lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(handler), **kwargs))
    prefix = tmp_path / '01-case-batch'
    settings = Settings(data_dir=tmp_path, deepseek_api_key='fake-evidence-key')
    if finish_reason == 'stop':
        assert closure.generate_with_evidence(settings, 'system', 'user', prefix) == body.strip()
    else:
        with pytest.raises(ModelError, match='未返回完整答案'):
            closure.generate_with_evidence(settings, 'system', 'user', prefix)
    assert len(requests) == 1
    assert prefix.with_suffix('.raw.txt').read_text(encoding='utf-8') == body
    metadata = prefix.with_suffix('.response.json').read_text(encoding='utf-8')
    assert json.loads(metadata)['finish_reason'] == finish_reason
    assert 'fake-evidence-key' not in metadata and '思考内容' not in metadata


def test_http_error_evidence_does_not_save_provider_error_body(tmp_path, monkeypatch):
    import httpx
    from app.config import Settings
    from app.deepseek import ModelError

    monkeypatch.setattr('openai.DefaultHttpxClient', lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            401, json={'error': {'message': 'fake-key-private-provider-error'}})), **kwargs))
    prefix = tmp_path / '01-case-batch'
    with pytest.raises(ModelError, match='密钥无效'):
        closure.generate_with_evidence(Settings(data_dir=tmp_path, deepseek_api_key='fake-key'),
                                       'system', 'user', prefix)
    assert not prefix.with_suffix('.raw.txt').exists()
    metadata = prefix.with_suffix('.response.json').read_text(encoding='utf-8')
    assert json.loads(metadata)['http_status'] == 401
    assert 'fake-key' not in metadata and 'private-provider-error' not in metadata
