param(
    [string]$Samples = 'D:\python\DocQA-test-files'
)
# 一次执行完整验证流程。任何关键步骤失败都停止，不自动重投未知状态的任务。
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location -LiteralPath $projectRoot
$outputRoot = Join-Path $projectRoot 'data\docling-validation'
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$statusPath = Join-Path $outputRoot 'stage1-status.json'
$logPath = Join-Path $outputRoot 'stage1.log'
$lockPath = Join-Path $outputRoot 'stage1.lock'
$lockStream = $null
$transcribing = $false
$phase = 'starting'
$started = (Get-Date).ToUniversalTime().ToString('o')
$composeArgs = @('compose', '--env-file', 'deploy/docling/lab.env', '-f', 'deploy/docling/compose.yaml', '-f', 'deploy/docling/compose.ocr-gpu.yaml')
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Set-Stage([string]$State, [string]$Detail) {
    # 状态与日志只写入 Git 忽略目录，不包含密钥。
    @{ state=$State; phase=$script:phase; detail=$Detail; started_at=$script:started;
       updated_at=(Get-Date).ToUniversalTime().ToString('o'); process_id=$PID } |
        ConvertTo-Json | Set-Content -LiteralPath $script:statusPath -Encoding utf8
}

function Invoke-Checked([string]$Program, [string[]]$CommandArgs) {
    & $Program @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw "步骤 $script:phase 失败，退出码 $LASTEXITCODE；请检查日志和样本结果。" }
}

try {
    # 独占文件锁防止重复启动导致切换设备或重复投递任务。
    $lockStream = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
    Start-Transcript -LiteralPath $logPath -Append | Out-Null
    $transcribing = $true
    if (-not (Test-Path -LiteralPath $Samples -PathType Container)) { throw '样本目录不存在' }
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PARSER_DEVICE = 'cuda'
    # 派生镜像只在本地构建；不向公共仓库拉取同名镜像，也不退回 CPU 版 ORT。
    $phase = 'checking_image'; Set-Stage 'running' '检查本地 OCR GPU 派生镜像，缺失时请先按 README 构建'
    Invoke-Checked 'docker' @('image', 'inspect', 'docqa/docling-ocr-gpu:1.34.0-ort1.30.0', '--format', '{{.Id}}')
    $phase = 'starting_gpu'; Set-Stage 'running' '启动 GPU 解析服务'
    Invoke-Checked 'docker' ($composeArgs + @('up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '180'))

    $phase = 'runtime_check'; Set-Stage 'running' '检查实际推理依赖和设备'
    $probe = 'import json,torch,onnxruntime as ort; print(json.dumps(dict(torch=torch.__version__,cuda_available=torch.cuda.is_available(),torch_cuda=torch.version.cuda,ort=ort.__version__,ort_providers=ort.get_available_providers())))'
    Invoke-Checked 'docker' ($composeArgs + @('exec', '-T', 'parser', 'python', '-c', $probe))
    & docker @composeArgs images --format json | Set-Content (Join-Path $outputRoot 'images.json') -Encoding utf8

    # 按用户要求跳过 CPU 基线，直接进行 GPU 样本验证。
    $phase = 'gpu-all'; Set-Stage 'running' 'GPU 配置逐份解析全部样本'
    Invoke-Checked $python @('scripts/validate_docling.py', $Samples, '--label', 'gpu-all')
    # 全样本可能切换或淘汰缓存中的 converter，不能直接将随后的扫描请求称为热运行。
    $phase = 'gpu-scan-prime'; Set-Stage 'running' '相同扫描请求预热，不计入稳定性能结论'
    Invoke-Checked $python @('scripts/validate_docling.py', $Samples, '--pattern', '04-*.pdf', '--label', 'gpu-scan-prime')
    foreach ($round in 1..3) {
        $phase = "gpu-scan-repeat-$round"; Set-Stage 'running' '同一服务进程、同一参数重复测量；仍需检查缓存日志'
        Invoke-Checked $python @('scripts/validate_docling.py', $Samples, '--pattern', '04-*.pdf', '--label', $phase)
    }
    & docker @composeArgs logs --no-color --tail 300 parser | Set-Content (Join-Path $outputRoot 'parser.log') -Encoding utf8
    $phase = 'awaiting_review'; Set-Stage 'completed' '自动转换结束；仍需人工核对版式、数字、完整性及恢复行为，不能据此宣称质量验收通过'
}
catch {
    # 未拿到锁时不覆盖正在运行的另一轮状态。
    if ($null -ne $lockStream) { Set-Stage 'failed' $_.Exception.Message }
    Write-Output $_.Exception.Message
    exit 1
}
finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($null -ne $lockStream) { $lockStream.Dispose() }
}
