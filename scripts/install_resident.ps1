# 显式运行才注册当前用户登录后的驻留任务；不迁移数据库、不修改模型配置。
param([Parameter(Mandatory=$true)][string]$DataDir,[int]$Port=8000,[switch]$Uninstall,[switch]$DryRun)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'local_service_common.ps1')
$resolvedDataDir=Resolve-LocalDataDir $DataDir $projectRoot
$python=Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$digest=[System.Security.Cryptography.SHA256]::Create()
try {$hash=[BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($resolvedDataDir.ToLowerInvariant()))).Replace('-','').Substring(0,16)} finally {$digest.Dispose()}
$taskName="DocQA-$hash"
$arguments="-X utf8 -m app.local_supervisor --data-dir `"$resolvedDataDir`" --port $Port"
$existing=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if($existing -and (@($existing.Actions).Count -ne 1 -or $existing.Actions[0].Execute -ne $python -or $existing.Actions[0].Arguments -ne $arguments -or $existing.Actions[0].WorkingDirectory -ne $projectRoot)){throw '同名计划任务身份不一致，未覆盖。'}
if($DryRun){Write-Output "任务 $taskName；目录 $resolvedDataDir；端口 $Port；仅检查，未安装或启动。";exit 0}
if($Uninstall){if($existing){Stop-ScheduledTask -TaskName $taskName;Unregister-ScheduledTask -TaskName $taskName -Confirm:$false};Write-Output '驻留任务已移除；现有三个角色仍需用 stop_local.ps1 停止。';exit 0}
if(-not (Test-Path -LiteralPath $python)){throw '请先准备项目虚拟环境。'}
$info=Get-LocalRuntimeInfo $projectRoot $resolvedDataDir
if(-not $info.database_exists -or -not $info.structure_valid -or $info.schema_version -ne $info.expected_schema_version){throw '请先完成备份和受控迁移，驻留任务不自动建库或升级。'}
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $projectRoot
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -Hidden
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output "已注册 $taskName。启动请 Start-ScheduledTask -TaskName $taskName；用户注销时任务退出，重新登录后恢复。"
