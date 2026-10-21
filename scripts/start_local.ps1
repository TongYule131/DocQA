# DocQA 本地启动脚本（Windows）
#
# 设计约束（对应任务书 §9）：
# 1. **启动前显式核对数据目录与端口**：脚本先解析并打印最终使用的 DOCQA_DATA_DIR，
#    再交给子进程；不允许“先启动默认应用再切目录”。
# 2. **只封装已验证的普通命令**：脚本本身不做任何特殊处理，等价于手动运行
#    uvicorn / python -m app.parse_worker / python -m app.analysis_worker。
# 3. **端口冲突只报告，不杀进程**：发现端口被占用时给出占用者（尽力而为）并退出，
#    绝不终止不属于本项目的进程。
# 4. **角色状态分开**：Web、解析 worker、分析 worker 是三个独立进程；
#    Docling 解析服务是独立 Docker 服务，本脚本默认不启动它。
# 5. 启动不新建任务、不探测模型账号；worker 仅恢复用户此前明确提交且可安全继续的任务。
#    有未确认调用的任务进入 needs_attention，不自动重发；已排队任务可能使用其剩余预算。
param(
    [string]$DataDir = "",
    [int]$Port = 8000,
    [switch]$NoParseWorker,
    [switch]$NoAnalysisWorker,
    [switch]$SkipMigrationCheck,
    [switch]$AllowDatabaseUpgrade,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'local_service_common.ps1')

$resolvedDataDir = Resolve-LocalDataDir $DataDir $projectRoot
$logDir = Join-Path $resolvedDataDir '.local-service'
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

Write-Host "=== DocQA 本地启动 ===" -ForegroundColor Cyan
Write-Host "项目根目录 : $projectRoot"
Write-Host "数据目录   : $resolvedDataDir"
Write-Host "Web 端口   : $Port"
Write-Host "说明       : 三个角色共用数据目录；已有旧库升级需显式 -AllowDatabaseUpgrade。"

if (-not (Test-Path $python)) {
    Write-Error "找不到虚拟环境 Python：$python。请先按 README 创建 .venv。"
    exit 1
}
Write-Host ("Python     : " + (& $python --version))

# 数据目录必须显式存在或可创建；绝不递归清空既有目录。
if (-not (Test-Path $resolvedDataDir)) {
    if ($DryRun) {
        Write-Host "[DryRun] 将创建数据目录：$resolvedDataDir"
    } else {
        New-Item -ItemType Directory -Path $resolvedDataDir | Out-Null
        Write-Host "已创建数据目录：$resolvedDataDir"
    }
}

# 端口占用检查：只报告，不杀进程。
$portOwner = $null
try {
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    if ($connections) {
        $ownerPid = ($connections | Select-Object -First 1).OwningProcess
        $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
        $portOwner = if ($owner) { "$($owner.ProcessName) (PID $ownerPid)" } else { "PID $ownerPid" }
    }
} catch {
    # 旧系统或权限不足时退化为 netstat 探测；失败不阻塞启动。
    $netstat = netstat -ano -p tcp | Select-String -Pattern ":$Port\s+.*LISTENING"
    if ($netstat) { $portOwner = "见 netstat 输出（未终止任何进程）" }
}
if ($portOwner) {
    Write-Error "端口 $Port 已被占用：$portOwner。请改用 -Port 指定其他端口；本脚本不会终止该进程。"
    exit 2
}
Write-Host "端口检查   : $Port 空闲" -ForegroundColor Green

# 兼容性保护不可由 SkipMigrationCheck 绕过；旧参数仅保留调用兼容。
$runtimeInfo = Get-LocalRuntimeInfo $projectRoot $resolvedDataDir
Write-Host ("数据库状态 : 当前 v{0} / 代码要求 v{1}" -f $runtimeInfo.schema_version, $runtimeInfo.expected_schema_version)
if (-not $runtimeInfo.structure_valid) { throw $runtimeInfo.schema_error }
if ($runtimeInfo.database_exists -and $runtimeInfo.schema_version -lt $runtimeInfo.expected_schema_version) {
    Write-Host '已有数据库需要增量升级；升级前会生成 SQLite 一致性备份。' -ForegroundColor Yellow
    if (-not $AllowDatabaseUpgrade -and -not $DryRun) {
        throw '未启动服务：请先核对数据目录和备份，再明确使用 -AllowDatabaseUpgrade。'
    }
}

if ($DryRun) {
    Write-Host "[DryRun] 预检完成，未启动任何进程。" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# 同一数据目录的启动串行化，且先核对全部角色，避免 Web 已启动才发现旧 worker。
$mutexDigest = [System.Security.Cryptography.SHA256]::Create()
$mutexHash = [BitConverter]::ToString($mutexDigest.ComputeHash([Text.Encoding]::UTF8.GetBytes($resolvedDataDir.ToLowerInvariant()))).Replace('-', '')
$mutexDigest.Dispose()
$startMutex = [System.Threading.Mutex]::new($false, "Local\DocQA-start-$mutexHash")
$ownsMutex = $false
$launchAttempted = $false
try {
    try { $ownsMutex = $startMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw '同一数据目录已有启动操作，请等待其完成。' }
    foreach ($roleName in @('web', 'parse-worker', 'analysis-worker')) {
        $existingRecordPath = Join-Path $logDir "$roleName.json"
        if (Test-Path -LiteralPath $existingRecordPath) {
            $existingRecord = Get-Content -LiteralPath $existingRecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($identity in @($existingRecord.processes) + @($existingRecord)) {
                if (Test-RoleProcess $identity) { throw "角色 $roleName 仍在运行，请先停止本数据目录的三个角色后重启。" }
            }
        }
    }

# 启动一个隐藏窗口的后台进程，并记录归属信息（PID、启动时间、可执行路径、角色、端口、数据目录）。
function Start-Role([string]$role, [string[]]$arguments, [int]$rolePort) {
    $recordPath = Join-Path $logDir "$role.json"
    if (Test-Path -LiteralPath $recordPath) {
        $oldRecord = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($identity in @($oldRecord.processes) + @($oldRecord)) {
            if (Test-RoleProcess $identity) { throw "角色 $role 仍在运行，请先停止本数据目录的服务。" }
        }
    }
    $env:DOCQA_DATA_DIR = $resolvedDataDir
    $env:DOCQA_EXPECTED_CODE_FINGERPRINT = $runtimeInfo.code_fingerprint
    $env:DOCQA_ALLOW_DATABASE_UPGRADE = if ($AllowDatabaseUpgrade) { '1' } else { '0' }
    $stdout = Join-Path $logDir "$role.out.log"
    $stderr = Join-Path $logDir "$role.err.log"
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Start-Sleep -Milliseconds 600
    $started = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
    $record = [ordered]@{
        role           = $role
        pid            = $process.Id
        started_at     = (Get-Date).ToUniversalTime().ToString("o")
        executable     = $python
        arguments      = $arguments -join " "
        port           = if ($rolePort -gt 0) { $rolePort } else { $null }
        data_dir       = $resolvedDataDir
        project_root   = $projectRoot
        stdout_log     = $stdout
        stderr_log     = $stderr
        start_time     = if ($started) { $started.StartTime.ToUniversalTime().ToString("o") } else { $null }
        processes      = @(Get-RoleProcessTree $process.Id)
        code_fingerprint = $runtimeInfo.code_fingerprint
        expected_schema_version = $runtimeInfo.expected_schema_version
    }
    $recordPath = Join-Path $logDir "$role.json"
    $record | ConvertTo-Json -Depth 4 | Set-Content -Path $recordPath -Encoding UTF8
    if (-not $started) {
        throw "角色 $role 启动后立即退出；请查看 $stderr"
    }
    $roleReady = $false
    for ($readyAttempt = 0; $readyAttempt -lt 30; $readyAttempt++) {
        $currentRuntime = Get-LocalRuntimeInfo $projectRoot $resolvedDataDir
        $readiness = Get-RoleReadiness $record $currentRuntime
        if ($readiness.ready) { $roleReady = $true; break }
        if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 200
    }
    if (-not $roleReady) { throw "角色 $role 未就绪或代码/数据库不一致，请查看 $stderr" }
    Write-Host ("已启动 {0,-16} PID {1,-7} 日志 {2}" -f $role, $process.Id, $stdout) -ForegroundColor Green
    return $process
}

# 启动顺序：Web 先启动（迁移由 Web 完成），再启动两个 worker。
$launchAttempted = $true
Start-Role "web" @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port") $Port | Out-Null
# 不能靠固定 600ms 假定迁移成功；Web 就绪后才启动会读取任务的 worker。
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 1 -UseBasicParsing
        # Windows PowerShell 5.1 对无 charset 的 JSON 可能按 ANSI 解码；
        # FastAPI 的 JSON 为 UTF-8，必须直接解码响应字节，避免中文目录误判不匹配。
        $health = [System.Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray()) | ConvertFrom-Json
        if ($response.StatusCode -eq 200 -and $health.runtime.data_dir -eq $resolvedDataDir -and
            $health.runtime.code_fingerprint -eq $runtimeInfo.code_fingerprint -and
            $health.runtime.schema_version -eq $runtimeInfo.expected_schema_version) { $ready = $true; break }
    } catch {
        # 团队模式保护 health 的运行详情；匿名认证状态只判断 HTTP 就绪，身份和版本仍核对本机角色记录。
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) {
            try {
                $authResponse = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/auth/me" -TimeoutSec 1 -UseBasicParsing
                $authState = [System.Text.Encoding]::UTF8.GetString($authResponse.RawContentStream.ToArray()) | ConvertFrom-Json
                $webRecord = Get-Content -LiteralPath (Join-Path $logDir 'web.json') -Raw -Encoding UTF8 | ConvertFrom-Json
                $webReadiness = Get-RoleReadiness $webRecord (Get-LocalRuntimeInfo $projectRoot $resolvedDataDir)
                if ($authResponse.StatusCode -eq 200 -and $authState.team_mode -eq $true -and $webReadiness.ready) {
                    $ready = $true
                    break
                }
            } catch { }
        }
        Start-Sleep -Milliseconds 300
    }
}
if (-not $ready) { throw "Web 未成功就绪；停止启动后续 worker，请查看迁移或启动日志。" }
if (-not $NoParseWorker) {
    Start-Role "parse-worker" @("-m", "app.parse_worker") 0 | Out-Null
}
if (-not $NoAnalysisWorker) {
    Start-Role "analysis-worker" @("-m", "app.analysis_worker") 0 | Out-Null
}

} catch {
    if ($launchAttempted) {
        # 预检时不存在存活的自身角色；失败只回收本轮已登记的进程，保留日志。
        & (Join-Path $PSScriptRoot 'stop_local.ps1') -DataDir $resolvedDataDir -KeepRecords
    }
    throw
} finally {
    if ($ownsMutex) { $startMutex.ReleaseMutex() }
    $startMutex.Dispose()
}

Write-Host ""
Write-Host "工作台地址 : http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "接口文档   : http://127.0.0.1:$Port/docs"
Write-Host "进程记录   : $logDir\*.json"
Write-Host "查看状态   : pwsh -File scripts\status_local.ps1 -DataDir `"$resolvedDataDir`""
Write-Host "停止       : pwsh -File scripts\stop_local.ps1 -DataDir `"$resolvedDataDir`""
Write-Host ""
Write-Host "注意：解析与摘要／提取任务分别由两个 worker 执行；未启动对应 worker 时任务只会停在 queued。"
Write-Host "      Docling 解析服务是独立 Docker 服务，本脚本不启动、不拉镜像、不下载模型。"
