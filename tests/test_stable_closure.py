"""稳定版收尾反例：真实 TXT 上传、解析、分批与原文引用；模型边界完全离线。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.analysis_jobs import AnalysisJobService
from app.analysis_worker import AnalysisWorker
from app.main import create_app
from app.parse_worker import ParseWorker
from app.repository import Repository
from tests.test_analysis import fixture_settings, reduce_reply, summary_reply


def upload_and_parse(client, repository, settings, text):
    response = client.post('/api/documents', files={'file': ('合成收尾样本.txt', text.encode(), 'text/plain')})
    assert response.status_code == 201
    document_id = response.json()['document']['id']
    response = client.post(f'/api/documents/{document_id}/parse')
    assert response.status_code == 202
    worker = ParseWorker(settings, repository)
    assert worker.run_task(response.json()['task']['id'])
    document = repository.get(document_id)
    assert document.active_parse_version_id
    return document


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_txt_paragraphs_preserve_complete_original_and_source_ranges(tmp_path, newline):
    settings = fixture_settings(tmp_path)
    repository = Repository(tmp_path / 'docqa.db')
    text = newline + ('首段保留 1.2 亿元及条件。' + newline * 2
                      + '第二段包含例外，未经审计。' + newline * 3 + '尾段不能丢失。' + newline * 2)
    with TestClient(create_app(settings)) as client:
        document = upload_and_parse(client, repository, settings, text)
    blocks = repository.blocks(document.active_parse_version_id)
    assert len(blocks) == 3
    assert ''.join(block.text for block in blocks) == text
    for block in blocks:
        source = block.sources[0]
        assert text[source.char_start:source.char_end] == block.text
        assert source.line_start == len(text[:source.char_start].splitlines()) + 1
        assert source.line_end >= source.line_start
        assert '不是物理 PDF' in source.note


def test_long_txt_upload_reaches_multibatch_reduce_and_keeps_last_chapter(tmp_path):
    settings = fixture_settings(tmp_path, deepseek_api_key='offline-test',
                                analysis_batch_max_chars=12000,
                                analysis_reduce_max_chars=16000, analysis_input_max_chars=20000)
    repository = Repository(tmp_path / 'docqa.db')
    facts = ['第一章：本项目仅覆盖华东直营门店，不含关联交易。',
             '第二章：材料未列示净利润，不能将缺失数据填为零。',
             '第三章：若外部供应中断，应暂停扩展。']
    text = '\n\n'.join(fact + '\n' + '背景说明应保留原文并核对来源。' * 350 for fact in facts)
    with TestClient(create_app(settings)) as client:
        document = upload_and_parse(client, repository, settings, text)
        service = AnalysisJobService(settings, repository)
        outcome = service.build_plan(document.id, 'summary')
        assert outcome.plan.executable
        assert len(outcome.plan.batches) >= 2 and outcome.plan.reduce_required
        response = client.post(f'/api/documents/{document.id}/summary', json={
            'parse_version_id': outcome.version_id, 'plan_fingerprint': outcome.plan.fingerprint,
            'idempotency_key': 'long-txt-complete'})
        assert response.status_code == 202
        job_id = response.json()['job']['id']

        class OfflineModel:
            def __init__(self):
                self.calls = []

            def generate(self, system_prompt, user_prompt):
                payload = json.loads(user_prompt)
                self.calls.append(payload)
                if 'units' in payload:
                    points, quotes = [], {}
                    for unit in payload['units']:
                        for fact in facts:
                            if fact in unit['content']:
                                points.append({'text': fact, 'refs': [unit['ref']]})
                                quotes[str(unit['ref'])] = fact
                    return summary_reply('合成试点范围与限制', points, [], quotes)
                points = [{'text': entry['text'], 'refs': [entry['item_id']]}
                          for entry in payload['items'] if entry['kind'] != 'overview']
                return reduce_reply('合成试点范围与限制', points, [])

        model = OfflineModel()
        AnalysisWorker(settings, repository, model=model).run_job(job_id)
        job = repository.get_analysis_job(job_id)
        assert job.status == 'succeeded', job.error_message
        result = client.get(f'/api/analysis-results/{job.result_id}').json()
        assert result['coverage']['complete'] and result['coverage']['reduce_completed']
        assert result['coverage']['processed_units'] == result['coverage']['total_units'] == 3
        assert len(model.calls) == outcome.plan.request_upper_bound == job.requests_used
        assert {point['text'] for point in result['summary']['main_points']} == set(facts)
        blocks = {block.id: block for block in repository.blocks(outcome.version_id)}
        for citation in result['summary']['citations']:
            assert citation['quote'] in blocks[citation['block_id']].text
            assert citation['sources'][0]['format'] == 'txt'


def test_long_single_paragraph_is_rejected_without_cutting_or_model_call(tmp_path):
    settings = fixture_settings(tmp_path, deepseek_api_key='offline-test')
    repository = Repository(tmp_path / 'docqa.db')
    text = '没有安全段落边界的长单段，不能截去末尾例外。' * 1000
    with TestClient(create_app(settings)) as client:
        document = upload_and_parse(client, repository, settings, text)
        plan = AnalysisJobService(settings, repository).build_plan(document.id, 'summary').plan
        assert not plan.executable and plan.blocked_reason == 'unit_over_batch_budget'
        assert len(plan.units) == 1 and plan.units[0].text == text
        assert plan.request_upper_bound == 0
