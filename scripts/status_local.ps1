# DocQA 本地服务状态查看（Windows）
#
# 只读脚本：读取由 scripts/start_local.ps1 写入的角色记录（PID、启动时间、可执行路径、
# 角色、端口、数据目录），并核对进程是否仍存活、身份是否匹配。
# 不会启动、停止或重启任何服务，也不会发起任何模型调用。
param(
    [string]$DataDir = "data",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'local_service_common.ps1')
if ([System.IO.Path]::IsPathRooted($DataDir)) {
    $resolvedDataDir = [System.IO.Path]::GetFullPath($DataDir)
} else {
    $resolvedDataDir = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $DataDir))
}
$logDir = Join-Path $resolvedDataDir '.local-service'

$records = @()
if (Test-Path $logDir) {
    foreach ($file in Get-ChildItem -Path $logDir -Filter "*.json" -File) {
        $record = Get-Content -Path $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        $identities = if ($record.processes) { @($record.processes) } else { @($record) }
        $processStates = @($identities | ForEach-Object {
            [ordered]@{ pid = $_.pid; running = [bool](Get-Process -Id $_.pid -ErrorAction SilentlyContinue)
                        identity_match = (Test-RoleProcess $_) }
        })
        $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        $matchesProject = $false
        $path = $null
        $startTime = $null
        if ($process) {
            try {
                $path = $process.Path
                $startTime = $process.StartTime.ToUniversalTime().ToString("o")
                # 身份核对：可执行路径必须等于记录中的 Python，且进程确实属于本项目。
                $matchesProject = Test-RoleProcess $record
            } catch {
                $matchesProject = $false
            }
        }
        $records += [ordered]@{
            role            = $record.role
            pid             = $record.pid
            running         = @($processStates | Where-Object { $_.running }).Count -gt 0
            identity_match  = @($processStates | Where-Object { $_.identity_match }).Count -gt 0
            processes       = $processStates
            port            = $record.port
            data_dir        = $record.data_dir
            recorded_start  = $record.start_time
            actual_start    = $startTime
            executable      = $record.executable
            log             = $record.stdout_log
            data_dir_match  = ($record.data_dir -eq $resolvedDataDir)
        }
    }
}

if ($AsJson) {
    $records | ConvertTo-Json -Depth 5
    exit 0
}

Write-Host "=== DocQA 本地服务状态 ===" -ForegroundColor Cyan
Write-Host "数据目录 : $resolvedDataDir"
if (-not $records.Count) {
    Write-Host "没有找到由 scripts/start_local.ps1 启动的进程记录。" -ForegroundColor Yellow
    Write-Host "（如果服务是手动启动的，本脚本无法判断归属；手动进程请查看其终端输出。）"
    exit 0
}
foreach ($item in $records) {
    $status = if ($item.running -and $item.identity_match) { "运行中" } `
              elseif ($item.running) { "运行中但身份不匹配（不会停止）" } else { "已退出" }
    $color = if ($item.running -and $item.identity_match) { "Green" } else { "Yellow" }
    Write-Host ("{0,-16} PID {1,-7} {2}" -f $item.role, $item.pid, $status) -ForegroundColor $color
    if ($item.port) { Write-Host ("                 端口 {0}" -f $item.port) }
    Write-Host ("                 数据目录匹配: {0} / 记录数据目录: {1}" -f $item.data_dir_match, $item.data_dir)
    Write-Host ("                 日志: {0}" -f $item.log)
}
Write-Host ""
Write-Host "说明：服务状态只表示进程存在；不代表模型账号可用或 Docling 解析服务可达。"
Write-Host "      解析服务可达性请查看工作台或 GET /api/parsing/status。"
