"""停止服务后的数据快照与隔离恢复；恢复永远只写入新的空目录。"""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3

from app.runtime import process_start_token
from app.migrations import validate_schema


def digest(path):
    value = sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def stopped(data_dir):
    for path in (data_dir / '.local-service').glob('*.ready'):
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        if record.get('process_start_token') and process_start_token(record.get('pid')) == record['process_start_token']:
            raise ValueError('services_must_be_stopped')
    supervisor = data_dir / '.local-service' / 'supervisor.json'
    if supervisor.exists():
        record = json.loads(supervisor.read_text(encoding='utf-8-sig'))
        if record.get('status') not in ('stopped', 'needs_attention') and process_start_token(record.get('pid')) == record.get('process_start_token'):
            raise ValueError('supervisor_must_be_stopped')


def backup(data_dir, destination):
    source, target = data_dir.resolve(), destination.resolve()
    if source == target or source in target.parents or target in source.parents or target.exists():
        raise ValueError('backup_requires_new_directory_outside_source')
    stopped(source)
    database = source / 'docqa.db'
    if not database.is_file():
        raise ValueError('database_missing')
    def database_marker():
        return [(p.name,digest(p)) for p in (database,Path(str(database)+'-wal')) if p.exists()]
    before_database=database_marker()
    files = [p for p in source.rglob('*') if p.is_file() and
             not any(part in {'.local-service', 'db-backups'} for part in p.relative_to(source).parts)
             and p.name not in {'docqa.db', 'docqa.db-wal', 'docqa.db-shm', 'docqa.db-journal'}]
    if any(p.is_symlink() or any(parent.is_symlink() for parent in p.parents if parent != source.parent) for p in files):
        raise ValueError('symlinks_not_supported')
    source_manifest={p.relative_to(source).as_posix():(p.stat().st_size,p.stat().st_mtime_ns) for p in files}
    target.mkdir(parents=True)
    entries = []
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(target / 'docqa.db')) as copy:
            original.backup(copy)
            version = validate_schema(copy)
            if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('backup_integrity_failed')
    entries.append({'path': 'docqa.db', 'sha256': digest(target / 'docqa.db')})
    for path in files:
        relative = path.relative_to(source)
        before = digest(path)
        out = target / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        if digest(out) != before or digest(path) != before:
            raise ValueError('source_changed_during_backup')
        entries.append({'path': relative.as_posix(), 'sha256': before})
    stopped(source)
    after_files=[p for p in source.rglob('*') if p.is_file() and
             not any(part in {'.local-service','db-backups'} for part in p.relative_to(source).parts)
             and p.name not in {'docqa.db','docqa.db-wal','docqa.db-shm','docqa.db-journal'}]
    if (before_database!=database_marker() or source_manifest!={p.relative_to(source).as_posix():(p.stat().st_size,p.stat().st_mtime_ns) for p in after_files}):
        raise ValueError('source_changed_during_backup')
    manifest = {'version': 'docqa-data-backup-v1', 'schema_version': version, 'files': entries}
    (target / 'backup-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def restore(backup_dir, destination):
    source, target = backup_dir.resolve(), destination.resolve()
    if target.exists() or source == target or source in target.parents:
        raise ValueError('restore_requires_new_directory')
    manifest = json.loads((source / 'backup-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 'docqa-data-backup-v1':
        raise ValueError('unsupported_backup')
    seen = set()
    for item in manifest['files']:
        relative = Path(item['path'])
        path = (source / relative).resolve()
        original=source/relative
        if (relative.is_absolute() or '..' in relative.parts or source not in path.parents
                or item['path'] in seen or original.is_symlink()
                or any(parent.is_symlink() for parent in original.parents if parent != source.parent)
                or digest(path) != item['sha256']):
            raise ValueError('backup_manifest_invalid')
        seen.add(item['path'])
    if 'docqa.db' not in seen:
        raise ValueError('backup_database_missing')
    target.mkdir(parents=True)
    for item in manifest['files']:
        out = target / item['path']
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / item['path'], out)
    with closing(sqlite3.connect(target / 'docqa.db')) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('restored_integrity_failed')
        validate_schema(db)
    return {'restored': True, 'file_count': len(seen), 'schema_version': manifest['schema_version'],
            'services_started': False, 'provider_calls': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('backup', 'restore'))
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(argv)
    result = backup(args.source, args.destination) if args.operation == 'backup' else restore(args.source, args.destination)
    print(json.dumps({'operation': args.operation, 'file_count': len(result.get('files', [])) if args.operation == 'backup' else result['file_count'], 'provider_calls': 0}))


if __name__ == '__main__':
    main()
