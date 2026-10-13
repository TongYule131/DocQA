# 进程身份同时核对路径和创建时间；PID 被复用时绝不误停其他 Python。
function Test-RoleProcess($identity) {
    if (-not $identity -or -not $identity.start_time) { return $false }
    $current = Get-Process -Id $identity.pid -ErrorAction SilentlyContinue
    if (-not $current) { return $false }
    try {
        return ($current.Path -eq $identity.executable -and
            $current.StartTime.ToUniversalTime().Ticks -eq ([datetime]$identity.start_time).ToUniversalTime().Ticks)
    } catch { return $false }
}

function Get-RoleProcessTree([int]$RootProcessId) {
    $identities = @()
    $current = Get-Process -Id $RootProcessId -ErrorAction SilentlyContinue
    if (-not $current) { return $identities }
    $identities += [ordered]@{
        pid = $current.Id
        executable = $current.Path
        start_time = $current.StartTime.ToUniversalTime().ToString('o')
    }
    # Windows venv Python 是转发进程，真正服务可能在其子进程里；一并记录后逆序停止。
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$RootProcessId")) {
        if ($child.Name -ieq 'python.exe' -or $child.Name -ieq 'pythonw.exe') {
            $identities += @(Get-RoleProcessTree $child.ProcessId)
        }
    }
    return $identities
}

# 三个入口遵循同一目录优先级：显式参数 > 环境变量 > 项目 .env > 项目 data。
function Resolve-LocalDataDir([string]$Value, [string]$ProjectRoot) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        $runtimePython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
        $Value = & $runtimePython -X utf8 -c @"
import os, sys
from pathlib import Path
from dotenv import dotenv_values
root = Path(sys.argv[1])
values = dotenv_values(root / '.env', encoding='utf-8-sig')
print(os.environ.get('DOCQA_DATA_DIR') or values.get('DOCQA_DATA_DIR') or 'data')
"@ $ProjectRoot
        if ($LASTEXITCODE -ne 0) { throw '无法读取本地数据目录配置。' }
    }
    if ([System.IO.Path]::IsPathRooted($Value)) { return [System.IO.Path]::GetFullPath($Value) }
    return [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $Value))
}

function Get-LocalRuntimeInfo([string]$ProjectRoot, [string]$DataDir) {
    Push-Location $ProjectRoot
    try {
        $runtimePython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
        $raw = & $runtimePython -X utf8 -m app.runtime --data-dir $DataDir
        if ($LASTEXITCODE -ne 0) { throw '运行身份预检失败。' }
        return ($raw | ConvertFrom-Json)
    } finally { Pop-Location }
}

function Get-RoleReadiness($Record, $RuntimeInfo) {
    $state = [ordered]@{ready=$false; code_match=$false; schema_match=$false;
        data_dir_match=$false; process_match=$false; schema_version=$null}
    $readyPath = Join-Path (Join-Path $RuntimeInfo.data_dir '.local-service') ($Record.role + '.ready')
    if (-not (Test-Path -LiteralPath $readyPath)) { return $state }
    try {
        $readyRecord = Get-Content -LiteralPath $readyPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $state.code_match = ($readyRecord.code_fingerprint -eq $RuntimeInfo.code_fingerprint)
        $state.schema_version = $readyRecord.schema_version
        $state.schema_match = ($RuntimeInfo.structure_valid -and
            $RuntimeInfo.schema_version -eq $RuntimeInfo.expected_schema_version -and
            $readyRecord.schema_version -eq $RuntimeInfo.expected_schema_version)
        $state.data_dir_match = ($readyRecord.data_dir -eq $RuntimeInfo.data_dir -and
            $readyRecord.database_path -eq (Join-Path $RuntimeInfo.data_dir 'docqa.db'))
        $identities = if ($Record.processes) { @($Record.processes) } else { @($Record) }
        $state.process_match = @($identities | Where-Object {
            $_.pid -eq $readyRecord.pid -and (Test-RoleProcess $_)
        }).Count -eq 1
        $state.ready = ($readyRecord.role -eq $Record.role -and $state.code_match -and
            $state.schema_match -and $state.data_dir_match -and $state.process_match)
    } catch { }
    return $state
}
