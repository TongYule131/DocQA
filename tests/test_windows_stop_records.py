"""损坏记录不能阻断其他已确认自身角色停机，也不能被自动删除。"""
import json
import os
import subprocess
import sys
import pytest
from tests.test_windows_launchers import shell,isolated_env,run_script,ROOT

pytestmark=pytest.mark.skipif(os.name!='nt',reason='Windows停机入口')


@pytest.mark.parametrize('broken',['supervisor','role_json','role_pid'])
def test_stop_reports_bad_record_but_still_stops_confirmed_own_role(shell,tmp_path,broken):
    data=tmp_path/'isolated-stop';directory=data/'.local-service';directory.mkdir(parents=True)
    child=subprocess.Popen([sys._base_executable,'-c','import time; time.sleep(120)'],
        env=isolated_env(data),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        command=f"$p=Get-Process -Id {child.pid}; @{{pid=$p.Id;executable=$p.Path;start_time=$p.StartTime.ToUniversalTime().ToString('o')}} | ConvertTo-Json"
        result=subprocess.run([shell,'-NoProfile','-Command',command],cwd=ROOT,env=isolated_env(data),capture_output=True,timeout=15)
        assert result.returncode==0
        record=json.loads(result.stdout.decode('utf-8-sig'));record.update(role='web',data_dir=str(data))
        (directory/'web.json').write_text(json.dumps(record),encoding='utf-8')
        bad=directory/('supervisor.json' if broken=='supervisor' else 'parse-worker.json')
        content='{incomplete' if broken!='role_pid' else json.dumps({'role':'parse-worker','data_dir':str(data),'pid':None})
        bad.write_text(content,encoding='utf-8');before=bad.read_bytes()
        result=run_script(shell,data,tmp_path,'stop-'+broken,'stop_local.ps1','-KeepRecords')
        assert result.returncode!=0  # 明确报告未能确认的记录，不能把部分停机宣称为全部成功。
        assert child.poll() is not None,'a corrupt record prevented stopping a confirmed own role'
        assert bad.read_bytes()==before and not (data/'docqa.db').exists()
        assert (directory/'web.json').exists()
        if broken=='supervisor':assert (directory/'supervisor.stop').exists()
    finally:
        if child.poll() is None:child.terminate()
        child.wait(timeout=10)
