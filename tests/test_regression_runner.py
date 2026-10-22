"""CI入口的失败传播、自动发现和隔离；不递归执行真实全量套件。"""
import json
import os
from pathlib import Path
import sys

import pytest

from scripts import run_regression as regression


def test_failed_command_does_not_hide_later_checks_or_turn_summary_green(tmp_path):
    output = tmp_path / 'reports'
    output.mkdir()
    marker = tmp_path / 'later-ran'
    plan = [regression.Check('first', [sys.executable, '-c', 'print("first failure"); raise SystemExit(7)']),
            regression.Check('dependent', [sys.executable, '-c', 'raise SystemExit(0)'], requires='first'),
            regression.Check('later', [sys.executable, '-c',
                             'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()'])]
    rows = regression.run_checks(plan, tmp_path, output, dict(os.environ))
    assert [r['status'] for r in rows] == ['failed', 'blocked', 'passed']
    assert rows[0]['exit_code'] == 7
    assert 'first failure' in (output / 'first.log').read_text()
    assert marker.exists()
    assert not regression.save_summary(rows, output)
    assert not json.loads((output / 'summary.json').read_text())['passed']


def test_missing_tool_and_timeout_are_failure_results_with_logs(tmp_path):
    missing = regression.execute(regression.Check('missing', [str(tmp_path / 'missing-tool')]),
                                 tmp_path, tmp_path, dict(os.environ))
    timeout = regression.execute(regression.Check('timeout',
        [sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.1),
        tmp_path, tmp_path, dict(os.environ))
    assert missing['status'] == 'failed'
    assert timeout['status'] == 'timed_out'
    assert (tmp_path / 'missing.log').is_file() and (tmp_path / 'timeout.log').is_file()


def test_new_web_regression_is_discovered_without_changing_runner(tmp_path):
    folder = tmp_path / 'tests/web'
    folder.mkdir(parents=True)
    with pytest.raises(ValueError, match='没有找到网页回归'):
        regression.checks(tmp_path, tmp_path, sys.executable)
    (folder / 'new_regression.mjs').touch()
    plan = regression.checks(tmp_path, tmp_path, sys.executable)
    assert any(c.name == 'web-new_regression' for c in plan)
    python = next(c for c in plan if c.name == 'pytest')
    assert python.command[1:4] == ['-m', 'pytest', '-q']
    assert not any(arg.startswith('tests/test_') for arg in python.command)


def test_local_environment_cannot_supply_production_data_or_provider_credentials(tmp_path, monkeypatch):
    for key in ('DEEPSEEK_API_KEY', 'EMBEDDING_API_KEY', 'DOCQA_DATA_DIR',
                'DOCQA_EXPECTED_CODE_FINGERPRINT', 'DOCQA_TEAM_MODE'):
        monkeypatch.setenv(key, 'synthetic-private-value')
    env = regression.offline_environment(tmp_path)
    assert env['DOCQA_DATA_DIR'] == str(tmp_path / 'runtime-data')
    assert env['DEEPSEEK_API_KEY'] == env['EMBEDDING_API_KEY'] == ''
    assert 'DOCQA_EXPECTED_CODE_FINGERPRINT' not in env
    assert 'DOCQA_TEAM_MODE' not in env
    assert 'synthetic-private-value' not in env.values()


def test_existing_report_directory_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(regression, 'ROOT', tmp_path)
    reports = tmp_path / 'data/existing'
    reports.mkdir(parents=True)
    first = reports / 'summary.json'
    first.write_text('first failure')
    monkeypatch.setattr(regression, 'checks', lambda *args: [])
    with pytest.raises(SystemExit) as error:
        regression.main(['--output-dir', str(reports)])
    assert error.value.code == 2
    assert first.read_text() == 'first failure'


def test_junit_preserves_skips_and_malformed_xml_cannot_pass(tmp_path):
    report = tmp_path / 'pytest.xml'
    report.write_text('<testsuites><testsuite tests="4" failures="0" errors="0" skipped="1"/></testsuites>')
    rows = [{'name': 'pytest', 'status': 'passed', 'seconds': 1}]
    assert regression.save_summary(rows, tmp_path)
    assert json.loads((tmp_path / 'summary.json').read_text())['pytest']['skipped'] == 1
    report.write_text('<truncated')
    assert not regression.save_summary(rows, tmp_path)
