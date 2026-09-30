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
    [string]$DataDir = "data",
    [int]$Port = 8000,
    [switch]$NoParseWorker,
    [switch]$NoAnalysisWorker,
    [switch]$SkipMigrationCheck,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'local_service_common.ps1')

function Resolve-DataDir([string]$value) {
    # 相对路径一律相对项目根解析，避免受调用者当前目录影响。
    if ([System.IO.Path]::IsPathRooted($value)) { return [System.IO.Path]::GetFullPath($value) }
    return [System.IO.Path]::GetFullPath((Join-Path $projectRoot $value))
}

$resolvedDataDir = Resolve-DataDir $DataDir
$logDir = Join-Path $resolvedDataDir '.local-service'
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

Write-Host "=== DocQA 本地启动 ===" -ForegroundColor Cyan
Write-Host "项目根目录 : $projectRoot"
Write-Host "数据目录   : $resolvedDataDir"
Write-Host "Web 端口   : $Port"
Write-Host "说明       : Web 与分析 worker 使用同一个 DOCQA_DATA_DIR；正式库升级不在本脚本范围内。"

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

# 迁移检查：只读取当前 schema 版本并说明是否需要迁移，不执行正式库写入以外的动作。
if (-not $SkipMigrationCheck) {
    $check = & $python -c @"
import json, sys
from pathlib import Path
from app import migrations
from app.repository import Repository
data_dir = Path(sys.argv[1])
db = data_dir / 'docqa.db'
if not db.exists():
    print(json.dumps({'exists': False, 'current': 0, 'target': migrations.SCHEMA_VERSION}))
else:
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        print(json.dumps({'exists': True, 'current': migrations.detect_version(conn),
                          'target': migrations.SCHEMA_VERSION}))
    finally:
        conn.close()
"@ $resolvedDataDir
    Write-Host "数据库状态 : $check"
    $parsed = $check | ConvertFrom-Json
    if ($parsed.exists -and $parsed.current -lt $parsed.target) {
        Write-Host "提示       : 该数据库需要从 v$($parsed.current) 迁移到 v$($parsed.target)。" -ForegroundColor Yellow
        Write-Host "             启动时会自动生成一致性备份；失败则停止启动并保留备份。"
        Write-Host "             请确认这不是正式业务库；本任务不授权对正式库执行升级。"
    }
}

if ($DryRun) {
    Write-Host "[DryRun] 预检完成，未启动任何进程。" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

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
    }
    $recordPath = Join-Path $logDir "$role.json"
    $record | ConvertTo-Json -Depth 4 | Set-Content -Path $recordPath -Encoding UTF8
    if (-not $started) {
        Write-Error "角色 $role 启动后立即退出；请查看 $stderr"
        return $null
    }
    Write-Host ("已启动 {0,-16} PID {1,-7} 日志 {2}" -f $role, $process.Id, $stdout) -ForegroundColor Green
    return $process
}

# 启动顺序：Web 先启动（迁移由 Web 完成），再启动两个 worker。
Start-Role "web" @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port") $Port | Out-Null
# 不能靠固定 600ms 假定迁移成功；Web 就绪后才启动会读取任务的 worker。
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/documents" -TimeoutSec 1 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 300 }
}
if (-not $ready) { throw "Web 未成功就绪；停止启动后续 worker，请查看迁移或启动日志。" }
if (-not $NoParseWorker) {
    Start-Role "parse-worker" @("-m", "app.parse_worker") 0 | Out-Null
}
if (-not $NoAnalysisWorker) {
    Start-Role "analysis-worker" @("-m", "app.analysis_worker") 0 | Out-Null
}

Write-Host ""
Write-Host "工作台地址 : http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "接口文档   : http://127.0.0.1:$Port/docs"
Write-Host "进程记录   : $logDir\*.json"
Write-Host "查看状态   : pwsh -File scripts\status_local.ps1 -DataDir $DataDir"
Write-Host "停止       : pwsh -File scripts\stop_local.ps1 -DataDir $DataDir"
Write-Host ""
Write-Host "注意：解析与摘要／提取任务分别由两个 worker 执行；未启动对应 worker 时任务只会停在 queued。"
Write-Host "      Docling 解析服务是独立 Docker 服务，本脚本不启动、不拉镜像、不下载模型。"
