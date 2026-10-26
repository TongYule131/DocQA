"""真实双PowerShell状态查询：非角色元数据、损坏记录和跨目录不得误报就绪。"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_windows_launchers import shell, isolated_env, read_status, ROOT

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows状态入口')


def records_dir(tmp_path):
    data = tmp_path / 'isolated status 中文'
    records = data / '.local-service'
    records.mkdir(parents=True)
    return data, records


def dead_record(data, role='parse-worker'):
    return dict(role=role, pid=2147483000, executable=sys.executable,
                start_time='2000-01-01T00:00:00Z', data_dir=str(data), port=None)


def hashes(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}


def test_status_ignores_non_role_state_files(shell, tmp_path):
    data, directory = records_dir(tmp_path)
    for name, text in {'last-backup.json':'{"backup":"private fixture"}',
                       'restart-window.json':'[123]', 'supervisor.json':'{"status":"ready"}',
                       'maintenance.json':'{not complete', 'other.json':'null'}.items():
        (directory/name).write_text(text, encoding='utf-8')
    before=hashes(directory)
    assert read_status(shell, data, tmp_path, 'metadata-only') == []
    assert hashes(directory)==before and not (data/'docqa.db').exists()


def test_status_keeps_other_roles_visible_when_one_record_is_corrupt(shell, tmp_path):
    data, directory = records_dir(tmp_path)
    (directory/'web.json').write_text('{partial json', encoding='utf-8')
    (directory/'parse-worker.json').write_text(json.dumps(dead_record(data)), encoding='utf-8')
    (directory/'analysis-worker.json').write_text(json.dumps(dead_record(data, 'web')), encoding='utf-8')
    before=hashes(directory)
    rows={r['role']:r for r in read_status(shell, data, tmp_path, 'corrupt-record')}
    assert set(rows)=={'web','parse-worker','analysis-worker'}
    assert rows['web']['record_error']=='record_unreadable'
    assert rows['analysis-worker']['record_error']=='role_mismatch'
    assert rows['parse-worker']['record_error'] is None
    assert all(not r['ready'] for r in rows.values())
    assert hashes(directory)==before and not (data/'docqa.db').exists()


@pytest.mark.parametrize('pid',[None,0,-1,'invalid',True,[],2**45,1.25])
def test_invalid_process_ids_are_reported_without_querying_or_writing(shell, tmp_path, pid):
    data, directory = records_dir(tmp_path);record=dead_record(data,'web');record['pid']=pid
    (directory/'web.json').write_text(json.dumps(record), encoding='utf-8');before=hashes(directory)
    rows=read_status(shell, data, tmp_path, 'invalid-pid')
    assert len(rows)==1 and rows[0]['record_error']=='invalid_pid'
    assert not rows[0]['running'] and not rows[0]['identity_match'] and not rows[0]['ready']
    assert hashes(directory)==before and not (data/'docqa.db').exists()


def test_invalid_child_process_record_does_not_abort_status(shell, tmp_path):
    data, directory = records_dir(tmp_path);record=dead_record(data,'web')
    record['processes']=[dict(pid=None,start_time='2000-01-01T00:00:00Z'),dead_record(data,'web')]
    (directory/'web.json').write_text(json.dumps(record),encoding='utf-8');before=hashes(directory)
    rows=read_status(shell,data,tmp_path,'invalid-child')
    assert len(rows)==1 and rows[0]['record_error']=='invalid_process_record' and not rows[0]['ready']
    assert len(rows[0]['processes'])==2 and not any(p['running'] for p in rows[0]['processes'])
    assert hashes(directory)==before and not (data/'docqa.db').exists()


@pytest.mark.parametrize('directory_value',[None, ['foreign']])
def test_invalid_directory_type_cannot_be_reported_as_a_match(shell,tmp_path,directory_value):
    data,directory=records_dir(tmp_path);record=dead_record(data,'web');record['data_dir']=directory_value
    (directory/'web.json').write_text(json.dumps(record),encoding='utf-8');before=hashes(directory)
    rows=read_status(shell,data,tmp_path,'invalid-directory')
    assert len(rows)==1 and rows[0]['record_error']=='invalid_data_dir'
    assert rows[0]['data_dir_match'] is False and rows[0]['ready'] is False
    assert hashes(directory)==before and not (data/'docqa.db').exists()


@pytest.mark.parametrize('scope',['same','foreign'])
def test_readiness_requires_record_directory_even_with_matching_live_identity(shell,tmp_path,scope):
    data,directory=records_dir(tmp_path)
    script=tmp_path/'readiness-scope.ps1'
    script.write_text(
        "$ErrorActionPreference='Stop'\n"
        ". (Join-Path $args[0] 'scripts/local_service_common.ps1')\n"
        "$data=$args[1]; $current=Get-Process -Id $PID\n"
        "$record=@{role='web';pid=$PID;executable=$current.Path;"
        "start_time=$current.StartTime.ToUniversalTime().ToString('o');data_dir=$data}\n"
        "if ($args[2] -eq 'foreign') {$record.data_dir=Join-Path $data 'other'}\n"
        "$ready=@{role='web';pid=$PID;data_dir=$data;database_path=(Join-Path $data 'docqa.db');"
        "schema_version=16;code_fingerprint='synthetic'}\n"
        "$path=Join-Path (Join-Path $data '.local-service') 'web.ready'\n"
        "$ready | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding UTF8\n"
        "$info=@{data_dir=$data;code_fingerprint='synthetic';structure_valid=$true;"
        "schema_version=16;expected_schema_version=16}\n"
        "Get-RoleReadiness $record $info | ConvertTo-Json\n", encoding='ascii')
    result=subprocess.run([shell,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
        str(ROOT),str(data),scope],cwd=ROOT,env=isolated_env(data),capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
    status=json.loads(result.stdout.decode('utf-8-sig'))
    assert status['ready'] is (scope=='same')
    assert not (data/'docqa.db').exists()
