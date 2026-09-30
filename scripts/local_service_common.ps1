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
