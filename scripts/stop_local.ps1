# DocQA 本地停止脚本（Windows）
#
# 安全约束（对应任务书 §9）：
# 1. **只停止由 scripts/start_local.ps1 创建的进程**：必须同时满足
#    - PID 存在于角色记录中；
#    - 记录中的可执行路径等于该进程当前的真实可执行路径（身份仍匹配）；
#    - 记录中的数据目录等于本次 -DataDir（防止停错环境）。
#    任何一项不满足都跳过并报告，绝不终止其它 Python／Docker 服务。
# 2. **终止与恢复**：Stop-Process 终止进程，并等待其结束；
#    worker 依靠已持久化的调用意图及租约恢复，不保证执行优雅退出清理。
# 3. 不停止 Docling 容器（它属于独立部署），需要时请按 README 手动 `docker compose stop`。
param(
    [string]$DataDir = "",
    [switch]$Force,
    [switch]$KeepSupervisor,
    [switch]$KeepRecords
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'local_service_common.ps1')
$resolvedDataDir = Resolve-LocalDataDir $DataDir $projectRoot
$logDir = Join-Path $resolvedDataDir '.local-service'
$invalidRecords = @()
# 手动停止必须先阻止监管器重启；监管器自身恢复时显式跳过此标志。
if (-not $KeepSupervisor -and (Test-Path -LiteralPath (Join-Path $logDir 'supervisor.json'))) {
    # 即使监管器记录损坏，也先设置本数据目录的手停标记，再处理可确认的角色。
    # 标记只作用于本目录，绝不按损坏记录中的PID终止其他进程。
    if (-not (Test-Path -LiteralPath (Join-Path $logDir 'supervisor.stop'))) {
        Set-Content -LiteralPath (Join-Path $logDir 'supervisor.stop') -Value 'operator_stop' -Encoding UTF8
    }
    try {
        $supervisorRecord=Get-Content -LiteralPath (Join-Path $logDir 'supervisor.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($supervisorRecord.data_dir -isnot [string]) { throw 'invalid_record' }
    } catch {
        $invalidRecords += (Join-Path $logDir 'supervisor.json')
        Write-Warning '监管器记录损坏，已保留并设置本目录手停标记；继续停止已确认的角色。'
    }
}

function Read-StopRecord([string]$Path, [string]$Role) {
    $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($value.role -isnot [string] -or $value.role -ne $Role -or $value.data_dir -isnot [string] -or
        -not (Get-RoleProcessId $value)) { throw 'invalid_role_record' }
    foreach ($identity in @(if ($value.processes) { $value.processes } else { $value })) {
        if (-not (Get-RoleProcessId $identity)) { throw 'invalid_process_record' }
    }
    return $value
}

# 停止顺序：先 worker 后 Web，避免 Web 停止后 worker 仍写入任务状态造成困惑。
$roleOrder = @("analysis-worker", "parse-worker", "web")

Write-Host "=== DocQA 本地停止 ===" -ForegroundColor Cyan
Write-Host "数据目录 : $resolvedDataDir"

$stopped = 0
$skipped = 0
foreach ($role in $roleOrder) {
    $recordPath = Join-Path $logDir "$role.json"
    if (-not (Test-Path $recordPath)) { continue }
    try { $record = Read-StopRecord $recordPath $role } catch {
        $invalidRecords += $recordPath
        Write-Warning "$role 记录无法核验，已保留且未停止其进程；继续检查其他角色。"
        $skipped += 1
        continue
    }
    if ($record.data_dir -ne $resolvedDataDir) {
        Write-Host ("{0,-16} 记录的数据目录为 {1}，与本次 {2} 不一致，跳过（不停止）。" -f `
            $role, $record.data_dir, $resolvedDataDir) -ForegroundColor Yellow
        $skipped += 1
        continue
    }
    # if 输出会展开单元素数组，必须在外层重新收集；兼容只有一个 PID 的旧记录。
    $identities = @(if ($record.processes) { $record.processes } else { $record })
    [array]::Reverse($identities)
    foreach ($identity in $identities) {
        if (-not (Test-RoleProcess $identity)) {
            Write-Host "$role PID $($identity.pid) 已退出或路径/创建时间不匹配，跳过。"
            $skipped += 1
            continue
        }
        # Stop-Process 是进程终止，不是 Ctrl+C；依靠持久化意图与租约恢复。
        Stop-Process -Id (Get-RoleProcessId $identity) -Force -ErrorAction SilentlyContinue
        Wait-Process -Id (Get-RoleProcessId $identity) -Timeout 15 -ErrorAction SilentlyContinue
    }
    if (-not (Get-Process -Id (Get-RoleProcessId $record) -ErrorAction SilentlyContinue)) {
        $stopped += 1
        Write-Host ("{0,-16} 已退出。" -f $role) -ForegroundColor Green
    }
}

if (-not $KeepRecords) {
    foreach ($role in $roleOrder) {
        $recordPath = Join-Path $logDir "$role.json"
        if (Test-Path $recordPath) {
            if ($recordPath -in $invalidRecords) { continue }
            try { $record = Read-StopRecord $recordPath $role } catch {
                $invalidRecords += $recordPath
                Write-Warning "$role 记录在清理前已变化，保留供核对。"
                continue
            }
            $identities = if ($record.processes) { @($record.processes) } else { @($record) }
            # 身份不匹配时保留记录供核实；不能把“拒绝停止”误当成“进程已退出”。
            $hasLiveProcess = @($identities | Where-Object {
                Get-Process -Id (Get-RoleProcessId $_) -ErrorAction SilentlyContinue
            }).Count -gt 0
            if ($record.data_dir -eq $resolvedDataDir -and -not $hasLiveProcess) {
                Remove-Item -LiteralPath $recordPath -Force
            }
        }
    }
}

Write-Host ""
Write-Host "已停止 $stopped 个角色，跳过 $skipped 个（身份或数据目录不匹配）。"
Write-Host "说明：已在途的生成请求可能继续在供应商侧计费；若任务停在 running，" `
    "下次启动或再次领取时会按租约恢复（有未确认调用则转 needs_attention，不自动重发）。"
Write-Host "      Docling 解析服务未受影响，需要停止请按 README 手动操作。"
if ($invalidRecords.Count) {
    Write-Warning '存在无法核验的记录，停机仅部分确认；未删除记录或终止未知进程，请人工核对。'
    exit 1
}
