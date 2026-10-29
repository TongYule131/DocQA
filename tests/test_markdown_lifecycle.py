"""N03可在CI重现的生产链路；只用合成原件、账号和模型协议桩。"""
import json
from pathlib import Path

from scripts.evaluate_markdown_lifecycle import run


def test_new_markdown_index_activation_keeps_old_answers_sources_and_failed_parse_boundary(tmp_path,monkeypatch):
    monkeypatch.setenv('DOCQA_DATA_DIR',str(tmp_path/'import-runtime'))
    old=tmp_path/'original';new=tmp_path/'new-policy'
    first=run(old);assert first['passed'] and first['provider_http']==0
    # 新分块输入只设在隔离副本，确保同解析器版本也产生不同内容地址，覆盖旧来源授权。
    second=run(new,old,chunk_max_chars=400,probe_failed_parse=True)
    assert second['passed'] and second['provider_http']==0 and second['old_results_unchanged']
    previous=json.loads((old/'report.json').read_text('utf-8'));current=json.loads((new/'report.json').read_text('utf-8'))
    assert previous['parse_version_id']!=current['parse_version_id'] and previous['index_id']!=current['index_id']
    assert current['parser_version']=='local-markdown-4' and current['original_sha256']==previous['original_sha256']
    assert current['overflow_probe']['status']=='failed' and current['overflow_probe']['provider_stub_delta']==0
    assert all(answer['stream_blocks'] and answer['snapshot']['parse_version_id']==current['parse_version_id'] for answer in current['answers'])
    assert all(b['heading_path'].startswith('客户端 C#') for b in current['blocks'])
    assert not Path(current['sentinel']).exists()
