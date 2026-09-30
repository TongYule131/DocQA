"""独立任务书验收反例：走真实编排/校验器，不调用在线服务。"""
import json
import pytest
from app.config import Settings
from app.rag_context import build_evidence
from app.rag_validation import validate_model_output, ModelOutputError
from app.schemas import QualityWarning
from app.repository import Repository
from test_rag_api import Gateway, indexed_client, seed_document, DOC
from test_rag_context import hit, search_result
from test_rag_validation import evidence_pack, payload
from test_rag_semantic_fixtures import _load_evaluator


@pytest.fixture
def gateway(monkeypatch):
    fake = Gateway()
    fake.install(monkeypatch)
    return fake


def test_budget_skips_large_first_hit_and_uses_later_candidate(tmp_path):
    result = search_result([hit('long', '长' * 1000, .9), hit('short', '短而有效', .8)])
    build = build_evidence(result, Settings(data_dir=tmp_path, rag_context_k=1), context_cap=400)
    assert [x.chunk_id for x in build.pack.items] == ['short']


def test_identical_text_in_different_sections_keeps_both_sources(tmp_path):
    a, b = hit('a', '利率为3%。', .9), hit('b', '利率为3%。', .8)
    a['heading_path'], b['heading_path'] = '甲银行', '乙银行'
    build = build_evidence(search_result([a, b]), Settings(data_dir=tmp_path))
    assert len(build.pack.items) == 2


def test_insufficient_must_reject_nonempty_evidence_quotes(tmp_path):
    raw = payload(status='insufficient_evidence', conclusion=[], explanation=[])
    with pytest.raises(ModelOutputError):
        validate_model_output(raw, evidence_pack(tmp_path))


def test_quote_must_not_strip_fabricated_boundary_spaces(tmp_path):
    raw = payload(evidence_quotes=[{'ref': 1, 'quote': ' 全书内容分为21个部分 '}])
    with pytest.raises(ModelOutputError):
        validate_model_output(raw, evidence_pack(tmp_path))


def test_block_warning_uses_block_id_not_chunk_id(tmp_path):
    h = hit('chunk', '公式无缓存', .9, page=None, sources=[{'format':'xlsx','sheet_name':'预算','cell_range':'D6'}])
    h['block_id'] = 'block'
    warning = QualityWarning(code='formula_cache_missing',message='公式未保存结果',scope='block',severity='warning',block_id='block')
    build = build_evidence(search_result([h]), Settings(data_dir=tmp_path), version_warnings=[warning])
    assert build.warnings == [warning]


def test_prompt_overhead_over_budget_is_422(tmp_path, gateway):
    for api, _ in indexed_client(tmp_path, gateway, rag_context_max_chars=10, rag_input_max_chars=100):
        response = api.post(f'/api/documents/{DOC}/questions', json={'question':'多少部分？'})
        assert response.status_code == 422
        assert response.json()['code'] == 'rag_input_too_large'
        assert not gateway.generation_requests


def test_old_index_uses_old_version_quality_warnings(tmp_path, gateway):
    for api, _ in indexed_client(tmp_path, gateway):
        repo = Repository(tmp_path/'docqa.db')
        old = repo.get(DOC).active_parse_version_id
        with repo.connect() as db:
            db.execute("INSERT INTO quality_warnings(id,document_id,parse_version_id,code,message,severity,scope,created_at) VALUES(?,?,?,?,?,'warning','document','now')", ('warn-old',DOC,old,'old_ocr','旧版 OCR 有限'))
        seed_document(tmp_path, [('new-c','新版本资料')], version_id='v-b')
        response = api.post(f'/api/documents/{DOC}/questions', json={'question':'多少部分？'})
        assert response.status_code == 200, response.text
        assert 'old_ocr' in {w['code'] for w in response.json()['quality_warnings']}


def test_invalid_indexed_version_rejected_when_preview_is_newer(tmp_path, gateway):
    for api, _ in indexed_client(tmp_path, gateway):
        repo = Repository(tmp_path/'docqa.db')
        old = repo.get(DOC).active_parse_version_id
        seed_document(tmp_path, [('new-c','新版本资料')], version_id='v-b')
        with repo.connect() as db:
            db.execute("UPDATE parse_versions SET quality_status='invalid' WHERE id=?", (old,))
        response = api.post(f'/api/documents/{DOC}/questions', json={'question':'多少部分？'})
        assert response.status_code == 409
        assert not gateway.generation_requests


def test_scope_cannot_trust_optional_search_document_id(tmp_path, gateway, monkeypatch):
    from app.vector_index import DocumentIndex
    original = DocumentIndex.search
    def corrupt(self, *args):
        result = original(self, *args)
        result.pop('document_id', None)
        result['results'][0]['document_id'] = 'other-doc'
        return result
    monkeypatch.setattr(DocumentIndex, 'search', corrupt)
    for api, _ in indexed_client(tmp_path, gateway):
        response = api.post(f'/api/documents/{DOC}/questions', json={'question':'多少部分？'})
        assert response.status_code == 409
        assert not gateway.generation_requests


def test_rag_threshold_does_not_change_original_search_api(tmp_path, gateway):
    for api, _ in indexed_client(tmp_path, gateway, rag_min_score=.99):
        response = api.post(f'/api/documents/{DOC}/search', json={'query':'其他问题'})
        assert response.status_code == 200
        assert response.json()['results']


def test_semantic_judge_rejects_wrong_reference_even_if_fact_exists_elsewhere():
    evaluator = _load_evaluator()
    case = {'question':'甲乙银行各多少人？','expected_status':'answered', 'must_include':[], 'must_not_include':[], 'allowed_refs':[1,2], 'evidence':[{'text':'甲银行九人。'},{'text':'乙银行十一人。'}]}
    answer = {'status':'answered','answer':'甲银行九人，乙银行十一人。',
              'conclusion':[{'text':'甲银行九人。','refs':[2]},{'text':'乙银行十一人。','refs':[1]}],
              'explanation':[], 'citations':[{'reference_id':1,'quote':'甲银行九人。'},{'reference_id':2,'quote':'乙银行十一人。'}]}
    assert not evaluator.judge(case, answer)['passed']


def test_online_budget_counts_embedding_batches_and_stops_before_extra_request():
    from types import SimpleNamespace
    from scripts.rag_eval_safety import RequestBudget
    evaluator = _load_evaluator()
    class Embed:
        settings = SimpleNamespace(embedding_batch_size=1)
        calls = 0
        def embed(self, texts):
            self.calls += 1
            return [[1, 0]] * len(texts)
    inner = Embed()
    budget = RequestBudget(1)
    counted = evaluator.CountingEmbedding(inner, budget)
    with pytest.raises(RuntimeError): counted.embed(['a', 'b'])
    assert inner.calls == counted.calls == budget.used == 1


def test_live_evaluator_requires_explicit_online_flag(tmp_path):
    import os, subprocess, sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1]/'scripts/evaluate_rag_live.py'
    result = subprocess.run([sys.executable, str(script), '--data-dir', str(tmp_path/'never-created')],
                            cwd=tmp_path, capture_output=True, env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    assert result.returncode == 2
    assert b'--allow-online' in result.stderr
    assert not (tmp_path/'never-created').exists()


def test_live_refresh_cannot_delete_formal_data(tmp_path, monkeypatch):
    from scripts.evaluate_rag_live import prepare_data_dir
    monkeypatch.setenv('DOCQA_DATA_DIR', str(tmp_path))
    sentinel = tmp_path/'keep.txt'; sentinel.write_text('keep')
    with pytest.raises(ValueError): prepare_data_dir(tmp_path, force=True)
    assert sentinel.read_text() == 'keep'


def test_evaluator_does_not_count_unexecuted_cases_as_validator_success():
    evaluator = _load_evaluator()
    summary = evaluator.summarize([{'case_id':'network-error', 'passed':False}])
    assert summary['validator_accepted'] == summary['validator_rejected'] == 0
    assert summary['validator_not_evaluated'] == 1


def test_secret_scan_includes_chinese_filenames(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from scripts import check_secrets
    monkeypatch.setattr(check_secrets, 'ROOT', tmp_path)
    def git_output(args, **kwargs):
        assert '-z' in args
        return SimpleNamespace(stdout='docs/中文报告.md\0README.md\0'.encode('utf-8'))
    monkeypatch.setattr(check_secrets.subprocess, 'run', git_output)
    assert check_secrets.git_visible_files() == [tmp_path/'docs/中文报告.md', tmp_path/'README.md']


def test_fact_newlines_cannot_detach_the_citation_when_rendered():
    from app.rag_validation import ValidatedFact, ValidatedOutput, render_markdown
    output = ValidatedOutput(status='answered', conclusion=[ValidatedFact('第一句。\n\n## 第二句。',[1])])
    lines = render_markdown(output).splitlines()
    assert lines == ['## 结论', '- 第一句。  ## 第二句。[1]']


def test_repeated_amount_in_explanation_still_needs_its_own_correct_reference():
    """保留 v3 在线失败：前面的结论引用正确，不能替补充说明中的错引免责。"""
    evaluator = _load_evaluator()
    case = {'question':'审批门槛和流程？', 'expected_status':'answered',
            'must_include':['十万元'], 'must_not_include':[], 'allowed_refs':[1,2],
            'evidence':[{'text':'采购金额超过十万元的，应当报总经理批准。'},
                        {'text':'采购需求的提出、比价与合同签署环节由采购管理制度分别规定。'}]}
    answer = {'status':'answered', 'answer':'采购金额超过十万元需总经理批准。',
              'conclusion':[{'text':'采购金额超过十万元需总经理批准。','refs':[1]}],
              'explanation':[{'text':'因此审批门槛为十万元，流程由采购管理制度规定。','refs':[2]}],
              'citations':[{'reference_id':1,'quote':case['evidence'][0]['text']},
                           {'reference_id':2,'quote':case['evidence'][1]['text']}]}
    assert not evaluator.judge(case, answer)['passed']


@pytest.mark.parametrize('reverse_refs', [False, True])
@pytest.mark.parametrize('claim,scope,token', [
    ('合成样本规定设备支出超过二十六万元需负责人批准。',
     '设备验收环节由设备管理细则另行规定。', '二十六万元'),
    ('合成样本规定收到申请后十四日内答复。',
     '申请材料清单由申请指南另行规定。', '十四日'),
    ('合成样本评审委员会由九人组成。',
     '评审委员的遴选流程由评审细则另行规定。', '九人'),
])
def test_compound_explanation_attribution_across_sources(tmp_path, claim, scope, token,
                                                       reverse_refs):
    """不同金额、期限、人数及编号顺序复现错引，避免只针对 S05 的十万元。

    错误/拆分/联合三种输出都走生产校验与渲染。形式合法不等于语义正确；
    正确的多片段联合引用仍应通过，不能借原子化建议收窄原有协议。
    """
    evaluator = _load_evaluator()
    texts = [scope, claim] if reverse_refs else [claim, scope]
    claim_ref, scope_ref = (2, 1) if reverse_refs else (1, 2)
    case = {
        'case_id': 'compound-attribution', 'category': '补充说明错引回归',
        'question': '规则及具体流程是什么？', 'expected_status': 'answered',
        'must_include': [token], 'must_not_include': [], 'allowed_refs': [1, 2],
        'evidence': [{'chunk_id': f'c{i}', 'text': text}
                     for i, text in enumerate(texts, start=1)],
        'simulated_model_output': {
            'status': 'answered', 'conclusion': [{'text': claim, 'refs': [claim_ref]}],
            'explanation': [{'text': claim + scope, 'refs': [scope_ref]}],
            'clarification_questions': [],
            'evidence_quotes': [{'ref': i, 'quote': text}
                                for i, text in enumerate(texts, start=1)],
        },
    }
    settings = Settings(data_dir=tmp_path)
    bad = evaluator.run_offline([case], settings)['records'][0]
    assert bad['validated'] is True
    assert bad['passed'] is False
    assert any(c['check'] == 'citation_attribution' and not c['passed']
               for c in bad['checks'])

    # 模拟作者按各自来源拆分说明；这只是协议/判定回归，不能冒充模型实测。
    case['simulated_model_output']['explanation'] = [{'text': scope, 'refs': [scope_ref]}]
    split = evaluator.run_offline([case], settings)['records'][0]
    assert split['passed'] is True
    assert split['answer']['explanation'] == [{'text': scope, 'refs': [scope_ref]}]

    case['simulated_model_output']['explanation'] = [
        {'text': claim + scope, 'refs': [claim_ref, scope_ref]}]
    joint = evaluator.run_offline([case], settings)['records'][0]
    assert joint['passed'] is True


def test_quoted_user_duration_keeps_strict_source_attribution(tmp_path):
    """保留 v5 实测未通过项：词面检查不豁免引号中的用户数字。

    这里的七天是复述用户条件，并非直接声明政策期限。现有判定不能区分二者，
    本轮不为得到通过结果而新增豁免；也不能将此测试称为完整语义判断。
    """
    evaluator = _load_evaluator()
    texts = [
        '样本公司退货规则（虚构样本）：自签收之日起七日内，商品未使用且不影响二次销售的，可以申请无理由退货。',
        '退货期限自签收之日起计算，购买日期不作为退货期限的起算点。',
    ]
    case = {
        'case_id': 'quoted-user-duration', 'category': '澄清说明来源回归',
        'question': '我买了七天了，还能无理由退货吗？',
        'expected_status': 'clarification_needed', 'must_include': ['签收'],
        'must_not_include': [], 'allowed_refs': [1, 2],
        'token_aliases': {'七日': ['七天']},
        'evidence': [{'chunk_id': f'c{i}', 'text': text}
                     for i, text in enumerate(texts, start=1)],
        'simulated_model_output': {
            'status': 'clarification_needed', 'conclusion': [],
            'explanation': [
                {'text': '无理由退货的期限自签收之日起计算，购买日期不作为起算点，因此“买了七天”不能直接对应可退货期限。',
                 'refs': [2]},
                {'text': '可以申请无理由退货的条件是：自签收之日起七日内，且商品未使用、不影响二次销售。',
                 'refs': [1]},
            ],
            'clarification_questions': ['请问您的签收日期是哪一天？', '商品是否未使用且不影响二次销售？'],
            'evidence_quotes': [{'ref': i, 'quote': text}
                                for i, text in enumerate(texts, start=1)],
        },
    }
    record = evaluator.run_offline([case], Settings(data_dir=tmp_path))['records'][0]
    assert record['validated'] is True
    assert record['passed'] is False
    assert any(c['check'] == 'citation_attribution' and not c['passed']
               for c in record['checks'])
