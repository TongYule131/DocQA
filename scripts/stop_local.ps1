# DocQA 本地停止脚本（Windows）
#
# 安全约束（对应任务书 §9）：
# 1. **只停止由 scripts/start_local.ps1 创建的进程**：必须同时满足
#    - PID 存在于角色记录中；
#    - 记录中的可执行路径等于该进程当前的真实可执行路径（身份仍匹配）；
#    - 记录中的数据目录等于本次 -DataDir（防止停错环境）。
#    任何一项不满足都跳过并报告，绝不终止其它 Python／Docker 服务。
# 2. **受控退出**：worker 收到停止请求后保留当前任务的可恢复状态；
#    本脚本发送 Ctrl+C 等价的停止信号（Stop-Process），并等待进程结束。
# 3. 不停止 Docling 容器（它属于独立部署），需要时请按 README 手动 `docker compose stop`。
param(
    [string]$DataDir = "data",
    [switch]$Force,
    [switch]$KeepRecords
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

# 停止顺序：先 worker 后 Web，避免 Web 停止后 worker 仍写入任务状态造成困惑。
$roleOrder = @("analysis-worker", "parse-worker", "web")

Write-Host "=== DocQA 本地停止 ===" -ForegroundColor Cyan
Write-Host "数据目录 : $resolvedDataDir"

$stopped = 0
$skipped = 0
foreach ($role in $roleOrder) {
    $recordPath = Join-Path $logDir "$role.json"
    if (-not (Test-Path $recordPath)) { continue }
    $record = Get-Content -Path $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
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
        Stop-Process -Id $identity.pid -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $identity.pid -Timeout 15 -ErrorAction SilentlyContinue
    }
    if (-not (Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) {
        $stopped += 1
        Write-Host ("{0,-16} 已退出。" -f $role) -ForegroundColor Green
    }
}

if (-not $KeepRecords) {
    foreach ($role in $roleOrder) {
        $recordPath = Join-Path $logDir "$role.json"
        if (Test-Path $recordPath) {
            $record = Get-Content -Path $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $identities = if ($record.processes) { @($record.processes) } else { @($record) }
            # 身份不匹配时保留记录供核实；不能把“拒绝停止”误当成“进程已退出”。
            $hasLiveProcess = @($identities | Where-Object {
                Get-Process -Id $_.pid -ErrorAction SilentlyContinue
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
