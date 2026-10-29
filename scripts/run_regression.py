"""分级离线回归入口；本地全量须经用户同意，定向检查不升级范围。"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def targeted_checks(root: Path, output: Path, python: str, plan) -> list[Check]:
    from scripts.regression_policy import clean_paths
    tests = clean_paths(plan.python_tests)
    web = clean_paths(plan.web_tests)
    if any(Path(p).parent.as_posix() != 'tests' or not Path(p).name.startswith('test_')
           or not p.endswith('.py') or not (root / p).is_file() for p in tests):
        raise ValueError('invalid_targeted_python_test')
    if any(Path(p).parent.as_posix() != 'tests/web' or not p.endswith('.mjs')
           or not (root / p).is_file() for p in web):
        raise ValueError('invalid_targeted_web_test')
    result = []
    if tests:
        result.append(Check('pytest-targeted', [python, '-m', 'pytest', '-q', '-ra',
                      '--junitxml=' + str(output / 'pytest.xml'), *tests], timeout=900))
    node = shutil.which('node') or 'node'
    result.extend(Check('web-' + Path(path).stem, [node, path], timeout=300) for path in web)
    result.extend([Check('docs', [python, 'scripts/check_docs.py', '--report', str(output / 'docs.json')]),
                   Check('diff', ['git', 'diff', '--check'])])
    return result


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]
    timeout: int = 180
    requires: str | None = None


def offline_environment(output: Path) -> dict[str, str]:
    # 环境变量优先于.env；不继承本机供应商设置、团队状态或正式数据目录。
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('DOCQA_', 'DEEPSEEK_', 'EMBEDDING_'))}
    env.update(DOCQA_DATA_DIR=str(output / 'runtime-data'),
               DOCQA_DOCLING_BASE_URL='http://127.0.0.1:9',
               DEEPSEEK_API_KEY='', EMBEDDING_API_KEY='',
               DEEPSEEK_BASE_URL='https://generation.invalid/v1',
               EMBEDDING_BASE_URL='https://embedding.invalid/v1',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return env


def checks(root: Path, output: Path, python: str) -> list[Check]:
    node = shutil.which('node') or 'node'
    result = [Check('dependencies', [python, '-m', 'pip', 'check']),
              Check('pytest', [python, '-m', 'pytest', '-q', '-ra',
                               # 完整套件在托管Windows上可能超过20分钟；超时仍失败且不自动重试。
                               '--junitxml=' + str(output / 'pytest.xml')], timeout=2400)]
    # 新增网页回归自动被发现；不得只维护一份容易落后的文件名清单。
    web = sorted((root / 'tests/web').glob('*.mjs'))
    if not web:
        raise ValueError('没有找到网页回归文件，不能报告全量通过')
    result.extend(Check('web-' + path.stem, [node, str(path)], timeout=300) for path in web)
    result.extend([
        Check('docs', [python, 'scripts/check_docs.py', '--report', str(output / 'docs.json')]),
        Check('rag-baseline', [python, 'scripts/evaluate_rag_quality.py', '--strategy', 'legacy',
                               '--report', str(output / 'rag-baseline.json')]),
        Check('rag-quality', [python, 'scripts/evaluate_rag_quality.py', '--gate',
                              '--baseline', str(output / 'rag-baseline.json'),
                              '--report', str(output / 'rag-quality.json')], requires='rag-baseline'),
        Check('answer-contract', [python, 'scripts/evaluate_answer_contract.py',
                                  '--report', str(output / 'answer-contract.json')]),
        Check('scope-retention', [python, 'scripts/evaluate_rag_scope.py',
                                  '--report', str(output / 'scope-retention.json')]),
        Check('diff', ['git', 'diff', '--check']),
    ])
    return result


def execute(check: Check, root: Path, output: Path, env: dict[str, str]) -> dict:
    started = time.monotonic()
    log = output / (check.name + '.log')
    status, code = 'failed', None
    print(f'RUN {check.name}', flush=True)
    with log.open('wb') as stream:
        try:
            completed = subprocess.run(check.command, cwd=root, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, timeout=check.timeout, check=False)
            code = completed.returncode
            status = 'passed' if code == 0 else 'failed'
        except subprocess.TimeoutExpired:
            status = 'timed_out'
            stream.write(b'\nRegression command timed out; not retried.\n')
        except OSError as error:
            stream.write(f'Cannot launch regression command: {error}\n'.encode('utf-8'))
    # Windows子进程写入同一日志的句柄行为由既有启停测试覆盖；这里不使用PIPE等待EOF。
    print(log.read_text(encoding='utf-8', errors='replace'), end='', flush=True)
    print(f'{status.upper()} {check.name}', flush=True)
    return {'name': check.name, 'status': status, 'exit_code': code,
            'seconds': round(time.monotonic() - started, 3), 'log': log.name}


def run_checks(plan: list[Check], root: Path, output: Path, env: dict[str, str]) -> list[dict]:
    rows = []
    for check in plan:
        if check.requires and not any(r['name'] == check.requires and r['status'] == 'passed' for r in rows):
            rows.append({'name': check.name, 'status': 'blocked', 'requires': check.requires})
        else:
            # 一个检查失败后继续运行其他独立检查，最终仍返回失败。
            rows.append(execute(check, root, output, env))
    return rows


def save_summary(rows: list[dict], output: Path, *, profile='full', selection=None) -> bool:
    passed = bool(rows) and all(row['status'] == 'passed' for row in rows)
    junit = None
    if (output / 'pytest.xml').is_file():
        try:
            suites = ET.parse(output / 'pytest.xml').getroot()
            suites = [suites] if suites.tag == 'testsuite' else list(suites)
            junit = {key: sum(int(s.get(key, 0)) for s in suites)
                     for key in ('tests', 'failures', 'errors', 'skipped')}
        except (ET.ParseError, ValueError):
            passed = False
    report = {'schema': 'docqa-' + profile + '-regression-v1', 'profile': profile,
              'selection': selection, 'passed': passed, 'checks': rows,
              'pytest': junit, 'python': platform.python_version(), 'platform': platform.platform(),
              'completed_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'offline engineering and synthetic gates; no live semantic evaluation'}
    (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# ' + ('Full regression' if profile == 'full' else 'Targeted checks'), '',
             '**' + ('PASSED' if passed else 'FAILED') + '**', '',
             '| Check | Result | Seconds |', '| --- | --- | --- |']
    lines.extend(f"| {r['name']} | {r['status']} | {r.get('seconds', '—')} |" for r in rows)
    if junit is not None:
        lines.extend(['', 'Pytest: ' + ', '.join(f'{k}={v}' for k, v in junit.items()) + '.'])
    lines.extend(['', 'Offline engineering and synthetic replay only; live model semantics are not evaluated.',
                  'Skipped tests remain visible in pytest.xml and the pytest log.'])
    (output / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return passed


def main(argv=None) -> int:
    # 用文件方式启动时也能导入同目录的纯策略模块，不导入业务应用。
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from dataclasses import asdict
    from scripts.regression_policy import Plan, git_paths, select
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True, help='本项目data内的新目录；已有目录拒绝覆盖')
    parser.add_argument('--profile', choices=('targeted', 'auto', 'full'), default='targeted')
    parser.add_argument('--changed-file', action='append', default=[], help='本次定向范围，可重复指定')
    parser.add_argument('--plan-file', type=Path, help='CI生成的变更范围及检查计划')
    parser.add_argument('--local-full-approved', action='store_true', help='仅在用户明确批准本次本地全量后使用')
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    if not output.is_relative_to(ROOT / 'data') or output == ROOT / 'data':
        parser.error('报告必须保存到本项目data内的独立新目录')
    if output.exists():
        parser.error('报告目录已存在，拒绝覆盖')
    try:
        if args.plan_file:
            selection = Plan(**json.loads(args.plan_file.read_text(encoding='utf-8')))
            if selection.profile not in {'targeted', 'full'}:
                raise ValueError('invalid_execution_profile')
        else:
            paths = args.changed_file or git_paths(ROOT, working=True)
            selection = select(ROOT, paths)
        profile = selection.profile if args.profile == 'auto' else args.profile
        if profile == 'full' and os.environ.get('GITHUB_ACTIONS') != 'true' and not args.local_full_approved:
            parser.error('本地全量须先取得用户明确同意；获准后才可加 --local-full-approved。可先使用 --profile targeted 做定向检查')
        plan = (checks(ROOT, output, sys.executable) if profile == 'full'
                else targeted_checks(ROOT, output, sys.executable, selection))
        output.mkdir(parents=True, exist_ok=False)
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))
    rows = run_checks(plan, ROOT, output, offline_environment(output))
    return 0 if save_summary(rows, output, profile=profile, selection=asdict(selection)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
