"""分级范围、CI去重和本地全量许可；全量执行器全部用桩替代。"""
import json
from pathlib import Path

import pytest

from scripts import regression_policy as policy
from scripts import run_regression as runner


@pytest.fixture
def root(tmp_path):
    for name in ('tests/test_profile.py', 'tests/test_knowledge_modules.py', 'tests/test_intent_routing.py',
                 'tests/test_rag_validation.py', 'tests/test_regression_policy.py', 'tests/test_api.py',
                 'tests/web/trace_status.mjs', 'tests/web/unified_workspace.mjs'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return tmp_path


@pytest.mark.parametrize('path', ['README.md', 'docs/方案.md', 'AGENTS.md'])
def test_documents_only_never_add_python_or_web_suites(root, path):
    plan = policy.select(root, [path])
    assert plan.profile == 'targeted'
    assert plan.python_tests == plan.web_tests == []


@pytest.mark.parametrize('path', ['app/web/style.css', 'app/web/unified-chat.html', 'app/web/knowledge-admin.js'])
def test_page_changes_only_select_web_checks(root, path):
    plan = policy.select(root, [path])
    assert plan.profile == 'targeted' and not plan.python_tests
    assert len(plan.web_tests) == 2


@pytest.mark.parametrize('path', ['app/migrations.py', 'app/product_access.py', 'app/rag_validation.py',
    'app/quote_spans.py', 'app/knowledge_tasks.py', 'app/knowledge_scope.py', 'app/provider_usage.py',
    'app/analysis_worker.py', 'app/web/auth-bridge.js', 'app/web/answer-stream.js', 'requirements.lock.txt',
    '.github/workflows/rag-quality.yml', 'scripts/run_regression.py'])
def test_core_changes_recommend_full_in_ci(root, path):
    plan = policy.select(root, [path])
    assert plan.profile == 'full' and plan.full_recommended and plan.reasons


@pytest.mark.parametrize(('path', 'expected'), [('app/profile.py', 'tests/test_profile.py'),
    ('app/intent_routing.py', 'tests/test_intent_routing.py'),
    ('app/knowledge_traces.py', 'tests/test_knowledge_modules.py')])
def test_independent_module_selects_its_tests(root, path, expected):
    plan = policy.select(root, [path])
    assert plan.profile == 'targeted' and expected in plan.python_tests


def test_multiple_backend_domains_and_unknown_files_escalate(root):
    assert policy.select(root, ['app/profile.py', 'app/intent_routing.py']).profile == 'full'
    assert policy.select(root, ['new-unmapped.conf']).profile == 'full'


def test_test_only_change_runs_that_test_and_discovery_keeps_new_cases(root):
    plan = policy.select(root, ['tests/test_profile.py'])
    assert plan.profile == 'targeted' and plan.python_tests == ['tests/test_profile.py']
    new = root / 'tests/test_rag_new_feature.py'
    new.touch()
    assert new.relative_to(root).as_posix() in policy.select(root, ['app/rag_context.py']).python_tests


@pytest.mark.parametrize('path', ['../outside.py', '/absolute.py', 'D:/outside.py', '-k', 'tests/../../secret'])
def test_changed_paths_cannot_escape_or_be_command_flags(path):
    with pytest.raises(ValueError):
        policy.clean_paths([path])


def test_feature_push_with_open_pr_skips_without_claiming_pass(root, monkeypatch):
    monkeypatch.setattr(policy, 'git_paths', lambda *args: pytest.fail('查重后不读取或运行套件'))
    plan = policy.ci_plan(root, {}, {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF_NAME': 'feature'}, lambda _: True)
    assert plan.profile == 'skip' and '不代表测试通过' in plan.reasons[0]


@pytest.mark.parametrize(('event_name', 'branch', 'expected'), [('push', 'feature', 'targeted'),
    ('push', 'main', 'full'), ('pull_request', 'feature', 'full')])
def test_ci_core_policy_varies_by_event(root, monkeypatch, event_name, branch, expected):
    monkeypatch.setattr(policy, 'git_paths', lambda *args: ['app/rag_validation.py'])
    plan = policy.ci_plan(root, {}, {'GITHUB_EVENT_NAME': event_name, 'GITHUB_REF_NAME': branch}, lambda _: False)
    assert plan.profile == expected


def test_ci_lookup_failure_does_not_skip_checks(root, monkeypatch):
    monkeypatch.setattr(policy, 'git_paths', lambda *args: ['app/web/style.css'])
    def unavailable(_):
        raise OSError('synthetic DNS failure')
    plan = policy.ci_plan(root, {}, {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF_NAME': 'feature'}, unavailable)
    assert plan.profile == 'targeted'
    assert any('查重不可用' in reason for reason in plan.reasons)


def test_release_tag_uses_full_without_branch_pr_lookup(root, monkeypatch):
    monkeypatch.setattr(policy, 'git_paths', lambda *args: ['README.md'])
    plan = policy.ci_plan(root, {}, {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF_NAME': 'v1.0', 'GITHUB_REF_TYPE': 'tag'},
                          lambda _: pytest.fail('标签不是开发分支'))
    assert plan.profile == 'full'


@pytest.mark.parametrize('requested', ['targeted', 'full'])
def test_manual_ci_range_is_explicit(root, monkeypatch, requested):
    monkeypatch.setattr(policy, 'git_paths', lambda *args: ['README.md'])
    plan = policy.ci_plan(root, {'inputs': {'profile': requested}}, {'GITHUB_EVENT_NAME': 'workflow_dispatch'})
    assert plan.profile == requested


def test_unapproved_local_full_stops_before_building_or_running_suites(root, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', root)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setattr(runner, 'checks', lambda *args: pytest.fail('不允许组装全量套件'))
    monkeypatch.setattr(runner, 'run_checks', lambda *args: pytest.fail('不允许执行套件'))
    with pytest.raises(SystemExit) as error:
        runner.main(['--profile', 'full', '--changed-file', 'README.md', '--output-dir', str(root / 'data/denied')])
    assert error.value.code == 2 and not (root / 'data/denied').exists()


def test_local_auto_cannot_silently_escalate_to_full(root, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', root)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    with pytest.raises(SystemExit):
        runner.main(['--profile', 'auto', '--changed-file', 'app/migrations.py', '--output-dir', str(root / 'data/auto')])
    assert not (root / 'data/auto').exists()


@pytest.mark.parametrize('context', ['ci', 'approved-local'])
def test_full_dispatch_allowed_only_with_ci_or_explicit_local_flag_using_stubs(root, monkeypatch, context):
    monkeypatch.setattr(runner, 'ROOT', root)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    if context == 'ci':
        monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    # 仅验证调度；不构造、更不执行真实全量命令。
    monkeypatch.setattr(runner, 'checks', lambda *args: [runner.Check('synthetic-full', ['unused'])])
    monkeypatch.setattr(runner, 'run_checks', lambda plan, *args: [{'name': plan[0].name, 'status': 'passed'}])
    output = root / 'data/full-stub'
    args = ['--profile', 'full', '--changed-file', 'README.md', '--output-dir', str(output)]
    if context == 'approved-local':
        args.append('--local-full-approved')
    assert runner.main(args) == 0
    assert json.loads((output / 'summary.json').read_text())['profile'] == 'full'


def test_targeted_execution_report_is_never_named_full(root, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', root)
    captured = []
    def fake_run(plan, *args):
        captured.extend(plan)
        return [{'name': check.name, 'status': 'passed'} for check in plan]
    monkeypatch.setattr(runner, 'run_checks', fake_run)
    output = root / 'data/targeted'
    assert runner.main(['--changed-file', 'tests/test_profile.py', '--output-dir', str(output)]) == 0
    report = json.loads((output / 'summary.json').read_text())
    assert report['profile'] == 'targeted' and report['schema'] == 'docqa-targeted-regression-v1'
    assert captured[0].command[-1] == 'tests/test_profile.py'
    assert 'Full regression' not in (output / 'summary.md').read_text()


def test_targeted_manifest_cannot_smuggle_an_arbitrary_program(root):
    plan = policy.select(root, ['README.md'])
    plan.python_tests = ['scripts/run_regression.py']
    with pytest.raises(ValueError):
        runner.targeted_checks(root, root / 'data/report', 'python', plan)


def test_ci_workflow_uses_policy_profile_and_never_local_approval_flag():
    text = (policy.ROOT / '.github/workflows/rag-quality.yml').read_text(encoding='utf-8')
    assert 'scripts/regression_policy.py --ci-event' in text
    assert '--profile $env:CHECK_PROFILE --plan-file' in text
    assert "steps.plan.outputs.profile != 'skip'" in text
    assert '--local-full-approved' not in text
