"""离线核对文档链接、冻结原文、版本表和配置样例；不导入应用或读取 .env。"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
import tomllib


def literal_constant(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f'Missing literal constant: {path.name}:{name}')


def source_consistency(root: Path) -> tuple[list[dict], int, int]:
    """读取源码字面量，不导入 main（导入可能装配实际数据库与供应商客户端）。"""
    issues, versions, defaults = [], {}, {}
    mapping = {
        '数据库 schema': ('app/migrations.py', 'SCHEMA_VERSION'),
        '任务式问答 Prompt': ('app/rag_prompts.py', 'TASK_PROMPT_VERSION'),
        '检索策略': ('app/retrieval_planning.py', 'RETRIEVAL_STRATEGY_VERSION'),
        '事实引用协议': ('app/rag_answer_contract.py', 'ANSWER_CONTRACT_VERSION'),
        '意图路由': ('app/intent_routing.py', 'ROUTING_VERSION'),
        '流式协议': ('app/answer_stream.py', 'STREAM_VERSION'),
        '知识库任务协议': ('app/knowledge_tasks.py', 'KNOWLEDGE_PROMPT_VERSION'),
    }
    for label, (relative, name) in mapping.items():
        if (root / relative).is_file():
            versions[label] = str(literal_constant(root / relative, name))
    package = root / 'pyproject.toml'
    if package.is_file():
        versions['项目包'] = tomllib.loads(package.read_text(encoding='utf-8-sig'))['project']['version']
        main = root / 'app/main.py'
        if main.exists():
            for node in ast.walk(ast.parse(main.read_text(encoding='utf-8-sig'))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'FastAPI':
                    for argument in node.keywords:
                        if argument.arg == 'version' and ast.literal_eval(argument.value) != versions['项目包']:
                            issues.append({'kind': 'api_package_version_mismatch', 'path': 'app/main.py'})
    readme = (root / 'README.md').read_text(encoding='utf-8-sig') if (root / 'README.md').is_file() else ''
    for label, expected in versions.items():
        match = re.search(r'^\|\s*' + re.escape(label) + r'\s*\|\s*`([^`]+)`\s*\|', readme, re.M)
        if not match or match[1] != expected:
            issues.append({'kind': 'readme_version_mismatch', 'path': 'README.md', 'field': label,
                           'expected': expected, 'actual': match[1] if match else None})
    config, example = root / 'app/config.py', root / '.env.example'
    if config.is_file():
        for node in ast.walk(ast.parse(config.read_text(encoding='utf-8-sig'))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'value'
                    and len(node.args) == 2 and all(isinstance(a, ast.Constant) for a in node.args)):
                defaults[node.args[0].value] = node.args[1].value
        values = dict(re.findall(r'^([A-Z][A-Z0-9_]*)=(.*)$', example.read_text(encoding='utf-8-sig'), re.M)) if example.exists() else {}
        for name, expected in defaults.items():
            actual = values.get(name)
            # ./data 与 data 指向相同的示例相对目录；不解析到当前机器的绝对路径。
            if name == 'DOCQA_DATA_DIR' and actual is not None:
                actual = Path(actual).as_posix()
            if actual is None or actual.strip() != expected:
                # 不记录样例中的取值，避免有人误把真实密钥填入样例时由检查器再次输出。
                issues.append({'kind': 'config_example_mismatch', 'path': '.env.example', 'field': name})
    return issues, len(versions), len(defaults)


def project_documents(root: Path) -> list[Path]:
    """只检查项目维护的文档；不遍历依赖缓存、node_modules或指向库外的目录联接。"""
    paths = set(root.glob('*.md'))
    excluded = {'node_modules', '.venv', 'site-packages', '__pycache__', '.git', '.pytest_cache'}
    for parent in (root/'docs', root/'deploy', root/'skills', root/'tests/fixtures'):
        for folder, directories, files in os.walk(parent, followlinks=False):
            directory = Path(folder)
            directories[:] = [name for name in directories if name not in excluded
                               and (directory/name).resolve().is_relative_to(root)]
            paths.update(directory/name for name in files if name.lower().endswith('.md'))
    return sorted(paths)


def archive_records(root: Path) -> list[dict]:
    path = root / 'docs/archive/manifest.json'
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('schema_version') != 'docqa-doc-archive-v1' or not isinstance(payload.get('files'), list):
        raise ValueError('Invalid document archive manifest')
    return payload['files']


def contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Document path escapes project root')
    return path


def resolve_archived_document(root: Path, original: str, expected_digest: str | None = None) -> Path:
    """旧冻结清单继续使用原路径；归档仅改存放位置，哈希保持不变。"""
    path = contained(root, original)
    if path.exists() and (expected_digest is None or hashlib.sha256(path.read_bytes()).hexdigest()==expected_digest):
        return path
    for record in archive_records(root):
        if record['original'] == original.replace('\\', '/') and (expected_digest is None or record['sha256']==expected_digest):
            return contained(root, record['archived'])
    return path


def check(root: Path, freezes: list[Path] | None = None) -> dict:
    root = root.resolve()
    issues, deferred = [], []
    archived_count = 0
    snapshots=set()
    for record in archive_records(root):
        path = contained(root, record['archived'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            issues.append({'kind':'archive_hash_mismatch','path':record['archived']})
        archived_count += 1
        if record.get('role')=='historical_status_snapshot':
            if not path.is_relative_to(root/'docs/archive'):raise ValueError('Historical snapshots must be in docs/archive')
            snapshots.add(path)
    paths = project_documents(root)
    links = 0
    for path in paths:
        # 状态整合前快照保持原字节及当时目录语义；当前文档链接仍全部检查。
        if path in snapshots:continue
        text = path.read_text(encoding='utf-8-sig')
        # 代码示例里的占位Markdown不作为文档导航链接。
        text = re.sub(r'```.*?```', '', text, flags=re.S)
        for target in re.findall(r'!?\[[^\]\n]*\]\(([^\n]+?)\)', text):
            target = target.strip().strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith('#'):
                continue
            relative = unquote(parsed.path)
            if not relative:
                continue
            resolved = (path.parent / relative).resolve()
            links += 1
            if not resolved.is_relative_to(root):
                issues.append({'kind':'link_outside_project','path':str(path.relative_to(root)),'target':target})
            elif not resolved.exists():
                # 本地模型响应、原件与截图受Git忽略；换机缺证据单列，不伪装为代码缺失。
                if resolved.is_relative_to(root/'data'):
                    deferred.append({'path':str(path.relative_to(root)),'target':target})
                else:
                    issues.append({'kind':'broken_link','path':str(path.relative_to(root)),'target':target})
    frozen_refs = 0
    for freeze in freezes or []:
        payload = json.loads(freeze.read_text(encoding='utf-8'))
        for relative, digest in payload.get('hashes', {}).items():
            relative = relative.replace('\\', '/')
            if not relative.startswith('docs/'):
                continue
            path = resolve_archived_document(root, relative, digest)
            frozen_refs += 1
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                issues.append({'kind':'frozen_document_mismatch','freeze':str(freeze),'path':relative})
    source_issues, version_count, config_count = source_consistency(root)
    issues.extend(source_issues)
    return {'passed':not issues,'documents':len(paths),'links_checked':links,'archives_verified':archived_count,
            'source_versions_checked':version_count, 'config_defaults_checked':config_count,
            'frozen_document_refs_verified':frozen_refs,'issues':issues,'deferred_local_evidence':deferred,
            'snapshot_links_preserved':len(snapshots),'provider_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--freeze',type=Path,action='append',default=[])
    parser.add_argument('--report',type=Path)
    args=parser.parse_args();result=check(args.root,args.freeze)
    if args.report:
        with args.report.open('x',encoding='utf-8') as file:json.dump(result,file,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
