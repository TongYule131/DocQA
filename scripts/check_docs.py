"""离线检查当前文档链接和冻结原文哈希；不导入应用或读取模型配置。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


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


def resolve_archived_document(root: Path, original: str) -> Path:
    """旧冻结清单继续使用原路径；归档仅改存放位置，哈希保持不变。"""
    path = contained(root, original)
    if path.exists():
        return path
    for record in archive_records(root):
        if record['original'] == original.replace('\\', '/'):
            return contained(root, record['archived'])
    return path


def check(root: Path, freezes: list[Path] | None = None) -> dict:
    root = root.resolve()
    issues, deferred = [], []
    archived_count = 0
    for record in archive_records(root):
        path = contained(root, record['archived'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            issues.append({'kind':'archive_hash_mismatch','path':record['archived']})
        archived_count += 1
    paths = sorted([*(root/'docs').rglob('*.md'), *(root/'deploy').rglob('*.md'),
                    *(root/'skills').rglob('*.md')])
    if (root/'README.md').exists():
        paths.append(root/'README.md')
    links = 0
    for path in paths:
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
            path = resolve_archived_document(root, relative)
            frozen_refs += 1
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                issues.append({'kind':'frozen_document_mismatch','freeze':str(freeze),'path':relative})
    return {'passed':not issues,'documents':len(paths),'links_checked':links,'archives_verified':archived_count,
            'frozen_document_refs_verified':frozen_refs,'issues':issues,'deferred_local_evidence':deferred,
            'provider_calls':0}


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
