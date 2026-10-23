"""按实际改动选择离线检查；CI 可分级，本地全量仍须用户明确同意。"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
POLICY_VERSION = 'risk-based-regression-v1'

# 这些测试是受影响模块的验证集合，不代表完整产品验收。
GROUPS = {
    'rag': ('test_rag_*.py', 'test_quote_spans.py', 'test_context_segment_contract.py',
            'test_statement_locators.py', 'test_task_answer_protocol.py'),
    'parse': ('test_parse_*.py', 'test_docling_validation.py', 'test_new_document_failures.py',
              'test_improvement_structure.py'),
    'tasks': ('test_knowledge_*.py', 'test_question_*.py', 'test_answer_stream*.py',
              'test_unified_knowledge.py'),
    'auth': ('test_product_access.py', 'test_product_workspace.py', 'test_password_policy.py',
             'test_admin_product.py', 'test_start_team_mode.py'),
    'analysis': ('test_analysis*.py', 'test_extraction_requirements.py', 'test_improvement_analysis_units.py'),
    'conversation': ('test_conversations.py', 'test_conversation_admission.py', 'test_unified_knowledge.py'),
    'intent': ('test_intent_routing.py', 'test_knowledge_modules.py'),
    'trace': ('test_knowledge_modules.py',),
    'profile': ('test_profile.py',),
    'ci': ('test_regression_runner.py', 'test_regression_policy.py', 'test_docs_checks.py'),
    'runtime': ('test_runtime_*.py', 'test_windows_launchers.py', 'test_migration_and_versions.py'),
    'api': ('test_api.py', 'test_acceptance_regressions.py'),
}


@dataclass
class Plan:
    version: str
    profile: str
    changed_files: list[str]
    python_tests: list[str]
    web_tests: list[str]
    reasons: list[str]
    full_recommended: bool = False


def clean_paths(paths):
    result = []
    for value in paths:
        value = value.replace('\\', '/')
        path = PurePosixPath(value)
        if not value or path.is_absolute() or '..' in path.parts or ':' in value or value.startswith('-'):
            raise ValueError('invalid_changed_path')
        if value not in result:
            result.append(value)
    return sorted(result)


def select(root: Path, paths: list[str]) -> Plan:
    paths = clean_paths(paths)
    groups, tests, web, reasons, core = set(), set(), set(), [], []
    backend_groups = set()
    all_web = {p.relative_to(root).as_posix() for p in (root / 'tests/web').glob('*.mjs')}
    for path in paths:
        p = PurePosixPath(path)
        stem = p.stem
        if path.startswith(('docs/', 'skills/')) or p.suffix == '.md':
            continue
        if path.startswith('tests/web/') and p.suffix == '.mjs':
            if (root / path).is_file():
                web.add(path)
            else:
                core.append('删除网页回归须复核验证范围')
            continue
        if path.startswith('tests/') and p.name.startswith('test_') and p.suffix == '.py':
            if (root / path).is_file():
                tests.add(path)
            else:
                core.append('删除Python回归须复核验证范围')
            continue
        if path.startswith('app/web/'):
            # 共享样式/控制器会影响多个页面，执行现有网页组，不牵连全部Python测试。
            web.update(all_web)
            if p.name in {'auth-bridge.js', 'api-client.js', 'answer-stream.js'}:
                core.append('共享认证、请求或流式协议变化')
                groups.add('tasks' if p.name == 'answer-stream.js' else 'auth')
            continue
        if path in {'pyproject.toml', 'requirements.lock.txt', '.env.example', 'pytest.ini', 'conftest.py'} or path.startswith('.github/'):
            core.append('依赖、全局配置或CI基础设施变化')
            groups.add('ci')
            continue
        if path.startswith('scripts/'):
            if stem in {'run_regression', 'regression_policy', 'check_docs'}:
                core.append('回归或检查入口变化'); groups.add('ci')
            elif stem in {'start_local', 'stop_local', 'status_local', 'local_service_common'}:
                core.append('服务启停与运行基础设施变化'); groups.add('runtime')
            elif 'rag' in stem or 'answer_contract' in stem:
                groups.add('rag')
            elif 'ocr' in stem or 'pdf' in stem:
                groups.add('parse')
            else:
                core.append('未映射的脚本变化，CI采用保守范围'); groups.add('api')
            continue
        if path.startswith('app/') and p.suffix == '.py':
            group = ('rag' if stem.startswith('rag') or stem in {'retrieval_planning', 'quote_spans', 'vector_index'}
                     else 'parse' if stem.startswith(('parse', 'pdf', 'docling')) or stem in {'chunking', 'document_normalizer', 'parsing'}
                     else 'intent' if stem.startswith('intent')
                     else 'trace' if stem == 'knowledge_traces'
                     else 'profile' if stem == 'profile'
                     else 'auth' if stem.startswith(('product_', 'admin_'))
                     else 'analysis' if stem.startswith(('analysis', 'extraction', 'summarization'))
                     else 'conversation' if stem.startswith('conversation')
                     else 'tasks' if stem.startswith(('knowledge', 'question', 'answer_stream', 'ingestion', 'unified'))
                     else 'runtime' if stem in {'migrations', 'repository', 'runtime', 'config'}
                     else 'api')
            groups.add(group); backend_groups.add(group)
            if group in {'rag', 'parse', 'auth', 'runtime', 'api'} or stem.endswith(('_schema', '_worker', '_jobs', '_tasks')) or stem in {'knowledge_scope', 'knowledge_questions', 'ingestion', 'answer_stream', 'answer_stream_store', 'answer_stream_routes', 'provider_usage', 'deepseek', 'embedding', 'providers'}:
                core.append('共享核心、权限、数据或任务契约变化')
            continue
        core.append('未映射的文件变化，CI采用保守范围'); groups.add('api')
    if len(backend_groups) > 1:
        core.append('多个后端模块集成变化')
    for group in sorted(groups):
        for pattern in GROUPS[group]:
            tests.update(p.relative_to(root).as_posix() for p in (root / 'tests').glob(pattern))
        if group in {'auth', 'tasks', 'conversation', 'ci'}:
            web.update(all_web)
        if group == 'trace' and (root / 'tests/web/trace_status.mjs').is_file():
            web.add('tests/web/trace_status.mjs')
    reasons.extend(dict.fromkeys(core))
    if not reasons:
        reasons.append('按受影响文件执行文档、网页或模块定向检查')
    return Plan(POLICY_VERSION, 'full' if core else 'targeted', paths,
                sorted(tests), sorted(web), reasons, bool(core))


def git_paths(root, base=None, *, working=False):
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout
    if base:
        # 基线取自事件中的提交SHA；不把分支名拼入shell。
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', base):
            raise ValueError('invalid_base_sha')
        raw = git('diff', '--name-only', '-z', '--no-renames', base, 'HEAD', '--')
    elif working:
        raw = git('diff', '--name-only', '-z', '--no-renames', 'HEAD', '--')
        raw += git('ls-files', '--others', '--exclude-standard', '-z')
    else:
        raw = git('ls-tree', '-r', '--name-only', '-z', 'HEAD')
    return clean_paths([p.decode('utf-8') for p in raw.split(b'\0') if p])


def open_pull_request(env):
    repository = env['GITHUB_REPOSITORY']
    owner = repository.split('/')[0]
    query = urllib.parse.urlencode({'state': 'open', 'head': owner + ':' + env['GITHUB_REF_NAME'], 'per_page': 1})
    url = env.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/') + '/repos/' + repository + '/pulls?' + query
    headers = {'User-Agent': 'DocQA-Regression-Policy', 'Accept': 'application/vnd.github+json'}
    if env.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + env['GH_TOKEN']
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
        return bool(json.load(response))


def ci_plan(root, event, env, lookup=open_pull_request):
    name = env.get('GITHUB_EVENT_NAME')
    branch = env.get('GITHUB_REF_NAME', '')
    release = name == 'push' and env.get('GITHUB_REF_TYPE') == 'tag'
    lookup_failed = False
    if name == 'push' and branch != 'main' and not release:
        try:
            if lookup(env):
                return Plan(POLICY_VERSION, 'skip', [], [], [], ['该分支已有PR，检查交由PR事件执行；本条不代表测试通过'])
        except Exception:
            # 查重服务不可用只能多做定向，不能漏掉检查或声称复用成功。
            lookup_failed = True
    base = event.get('pull_request', {}).get('base', {}).get('sha') if name == 'pull_request' else event.get('before')
    if not base or set(base) == {'0'}:
        base = None
    plan = select(root, git_paths(root, base))
    requested = event.get('inputs', {}).get('profile', 'auto') if name == 'workflow_dispatch' else 'auto'
    if requested not in {'auto', 'targeted', 'full'}:
        raise ValueError('invalid_requested_profile')
    if release:
        plan.profile = 'full'
        plan.reasons.append('版本标签按阶段交付执行全量')
    elif name == 'push' and branch != 'main':
        plan.profile = 'targeted'
        plan.reasons.append('开发分支push只做定向；核心改动在PR或main执行全量')
        if lookup_failed:
            plan.reasons.append('PR查重不可用，保留定向检查，不报告复用')
    elif requested != 'auto':
        plan.profile = requested
        plan.reasons.append('手动选择检查范围：' + requested)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ci-event', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args(argv)
    plan = ci_plan(ROOT, json.loads(args.ci_event.read_text(encoding='utf-8')), os.environ)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(asdict(plan), ensure_ascii=False, indent=2), encoding='utf-8')
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8') as stream:
            stream.write('profile=' + plan.profile + '\n')
    print(json.dumps({'profile': plan.profile, 'reasons': plan.reasons}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
