[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Run', 'Watch', 'Status')]
    [string]$Action = 'Status',
    [string]$MySqlExe,
    [string]$MySqlDefaultsFile,
    [ValidateRange(1, 65535)]
    [int]$MySqlPort = 3306,
    [ValidateRange(1, 60)]
    [int]$WatchdogMinutes = 5
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeDir = Join-Path $projectRoot '.runtime'
$configFile = Join-Path $runtimeDir 'module1-autostart.json'
$logFile = Join-Path $runtimeDir 'module1-autostart.log'
$workerScript = Join-Path $PSScriptRoot 'module1-worker.ps1'
$taskName = 'Leedis Aftersales Module1 Watchdog'
$startupDir = [Environment]::GetFolderPath('Startup')
$startupFile = Join-Path $startupDir 'LeedisAftersalesModule1.lnk'
$watchdogPidFile = Join-Path $runtimeDir 'module1-autostart.pid'
$watchdogStopFile = Join-Path $runtimeDir 'module1-autostart.stop'

function Write-AutostartLog {
    param([string]$Message)
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $logFile -Value "[$timestamp] $Message" -Encoding utf8
}

function Get-PowerShellExecutable {
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf) {
        return $windowsPowerShell
    }
    $pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $pwsh) {
        return $pwsh.Source
    }
    throw '没有找到可用于登录自启动的 PowerShell 可执行文件'
}

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutMilliseconds = 1500
    )
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync($HostName, $Port)
        if (-not $connect.Wait($TimeoutMilliseconds)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Get-RunningMySqlConfiguration {
    $processes = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('mysqld.exe', 'mariadbd.exe') -and $_.CommandLine
    }
    foreach ($process in $processes) {
        $match = [regex]::Match(
            [string]$process.CommandLine,
            '--defaults-file=(?:"([^"]+)"|([^\s]+))'
        )
        if ($match.Success) {
            $defaultsFile = if ($match.Groups[1].Success) {
                $match.Groups[1].Value
            }
            else {
                $match.Groups[2].Value
            }
            return [pscustomobject]@{
                MySqlExe = [string]$process.ExecutablePath
                MySqlDefaultsFile = $defaultsFile
            }
        }
    }
    return $null
}

function Get-AutostartConfiguration {
    if (-not (Test-Path -LiteralPath $configFile)) {
        throw "缺少本机启动配置，请先执行：& .\scripts\module1-autostart.ps1 -Action Install"
    }
    return Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
}

function Get-Module1WorkerProcess {
    $pidFile = Join-Path $runtimeDir 'module1-worker.pid'
    if (-not (Test-Path -LiteralPath $pidFile)) {
        return $null
    }
    $workerPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$workerPid)) {
        return $null
    }
    $process = Get-Process -Id $workerPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    $expectedExe = Join-Path $projectRoot '.venv\Scripts\aftersales-run-module1.exe'
    try {
        if (-not [string]::Equals(
            [System.IO.Path]::GetFullPath($expectedExe),
            [System.IO.Path]::GetFullPath($process.Path),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            return $null
        }
    }
    catch {
        return $null
    }
    return $process
}

function Get-WatchdogProcess {
    if (-not (Test-Path -LiteralPath $watchdogPidFile)) {
        return $null
    }
    $watchdogPid = 0
    if (-not [int]::TryParse(
        (Get-Content -LiteralPath $watchdogPidFile -Raw).Trim(),
        [ref]$watchdogPid
    )) {
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$watchdogPid" -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.Name -notin @('powershell.exe', 'pwsh.exe')) {
        return $null
    }
    $scriptPath = [System.IO.Path]::GetFullPath($PSCommandPath)
    if (
        -not ([string]$process.CommandLine).Contains($scriptPath) -or
        -not ([string]$process.CommandLine).Contains('-Action Watch')
    ) {
        return $null
    }
    return $process
}

function Start-AftersalesRuntime {
    $config = Get-AutostartConfiguration
    $mysqlReady = Test-TcpPort -HostName $config.MySqlHost -Port $config.MySqlPort
    if (-not $mysqlReady) {
        if (-not (Test-Path -LiteralPath $config.MySqlExe -PathType Leaf)) {
            throw "MySQL 程序不存在：$($config.MySqlExe)"
        }
        if (-not (Test-Path -LiteralPath $config.MySqlDefaultsFile -PathType Leaf)) {
            throw "MySQL 配置文件不存在：$($config.MySqlDefaultsFile)"
        }
        $mysqlArgument = "--defaults-file=`"$($config.MySqlDefaultsFile)`""
        Start-Process `
            -FilePath $config.MySqlExe `
            -ArgumentList $mysqlArgument `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden | Out-Null
        Write-AutostartLog 'MySQL 未运行，已发出隐藏启动请求'
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            Start-Sleep -Seconds 1
            if (Test-TcpPort -HostName $config.MySqlHost -Port $config.MySqlPort) {
                $mysqlReady = $true
                break
            }
        }
        if (-not $mysqlReady) {
            throw "MySQL 在 60 秒内未监听 $($config.MySqlHost):$($config.MySqlPort)"
        }
    }

    $worker = Get-Module1WorkerProcess
    if ($null -eq $worker) {
        & $workerScript -Action Start | ForEach-Object { Write-AutostartLog $_ }
        $worker = Get-Module1WorkerProcess
        if ($null -eq $worker) {
            throw '模块1后台运行器启动后未通过进程核验'
        }
        Write-AutostartLog "模块1后台运行器守护启动成功，PID=$($worker.Id)"
    }
}

function Start-WatchdogProcess {
    $existing = Get-WatchdogProcess
    if ($null -ne $existing) {
        return $existing.ProcessId
    }
    Remove-Item -LiteralPath $watchdogStopFile -Force -ErrorAction SilentlyContinue
    $scriptPath = [System.IO.Path]::GetFullPath($PSCommandPath)
    $powershellExe = Get-PowerShellExecutable
    $arguments = (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden ' +
        "-File `"$scriptPath`" -Action Watch"
    )
    $process = Start-Process `
        -FilePath $powershellExe `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $watchdogPidFile -Value $process.Id -Encoding ascii
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        throw "模块1守护进程启动失败，请查看 $logFile"
    }
    return $process.Id
}

function Start-WatchdogLoop {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    Set-Content -LiteralPath $watchdogPidFile -Value $PID -Encoding ascii
    Remove-Item -LiteralPath $watchdogStopFile -Force -ErrorAction SilentlyContinue
    $config = Get-AutostartConfiguration
    Write-AutostartLog "无管理员权限守护进程已启动，PID=$PID"
    try {
        while (-not (Test-Path -LiteralPath $watchdogStopFile)) {
            try {
                Start-AftersalesRuntime
            }
            catch {
                Write-AutostartLog "守护检查失败：$($_.Exception.Message)"
            }
            $waitSeconds = [int]$config.WatchdogMinutes * 60
            for ($elapsed = 0; $elapsed -lt $waitSeconds; $elapsed++) {
                if (Test-Path -LiteralPath $watchdogStopFile) {
                    break
                }
                Start-Sleep -Seconds 1
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $watchdogPidFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $watchdogStopFile -Force -ErrorAction SilentlyContinue
        Write-AutostartLog '无管理员权限守护进程已停止'
    }
}

function Install-AutostartTask {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $detected = Get-RunningMySqlConfiguration
    if (-not $MySqlExe -and $null -ne $detected) {
        $script:MySqlExe = $detected.MySqlExe
    }
    if (-not $MySqlDefaultsFile -and $null -ne $detected) {
        $script:MySqlDefaultsFile = $detected.MySqlDefaultsFile
    }
    if (-not $MySqlExe -or -not $MySqlDefaultsFile) {
        throw '未检测到正在运行的 MySQL，请通过 -MySqlExe 和 -MySqlDefaultsFile 指定本机路径'
    }
    $resolvedMySqlExe = (Resolve-Path -LiteralPath $MySqlExe).Path
    $resolvedDefaultsFile = (Resolve-Path -LiteralPath $MySqlDefaultsFile).Path
    [ordered]@{
        MySqlExe = $resolvedMySqlExe
        MySqlDefaultsFile = $resolvedDefaultsFile
        MySqlHost = '127.0.0.1'
        MySqlPort = $MySqlPort
        WatchdogMinutes = $WatchdogMinutes
        InstalledAt = (Get-Date).ToString('s')
        InstalledBy = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    } | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding utf8

    $scriptPath = [System.IO.Path]::GetFullPath($PSCommandPath)
    $powershellExe = Get-PowerShellExecutable
    $taskArguments = (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden ' +
        "-File `"$scriptPath`" -Action Run"
    )
    $scheduledAction = New-ScheduledTaskAction `
        -Execute $powershellExe `
        -Argument $taskArguments `
        -WorkingDirectory $projectRoot
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn
    $watchdogTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal `
        -UserId $identity `
        -LogonType Interactive `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    $task = New-ScheduledTask `
        -Action $scheduledAction `
        -Trigger @($logonTrigger, $watchdogTrigger) `
        -Principal $principal `
        -Settings $settings `
        -Description '登录后启动并每 5 分钟守护利德仕售后工作台 MySQL 与模块1运行器'
    $installMode = 'scheduled-task'
    try {
        Register-ScheduledTask `
            -TaskName $taskName `
            -InputObject $task `
            -Force `
            -ErrorAction Stop | Out-Null
        Remove-Item -LiteralPath $startupFile -Force -ErrorAction SilentlyContinue
    }
    catch {
        if ($_.Exception.Message -notmatch 'Access is denied|拒绝访问|0x80070005') {
            throw
        }
        $installMode = 'startup-watchdog'
        New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
        $watchArguments = (
            '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden ' +
            "-File `"$scriptPath`" -Action Watch"
        )
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($startupFile)
        $shortcut.TargetPath = $powershellExe
        $shortcut.Arguments = $watchArguments
        $shortcut.WorkingDirectory = $projectRoot
        $shortcut.WindowStyle = 7
        $shortcut.Description = '利德仕售后工作台模块1登录自启动与5分钟守护'
        $shortcut.Save()
        $watchdogPid = Start-WatchdogProcess
        Write-AutostartLog "计划任务注册被拒绝，已回退为用户启动目录守护，PID=$watchdogPid"
    }
    Start-AftersalesRuntime
    if ($installMode -eq 'scheduled-task') {
        Write-Output "已安装计划任务：$taskName"
        Write-Output "登录触发 + 每 $WatchdogMinutes 分钟计划任务守护；运行日志：$logFile"
    }
    else {
        Write-Output "计划任务权限不足，已安装当前用户登录启动项：$startupFile"
        Write-Output "隐藏进程每 $WatchdogMinutes 分钟守护；运行日志：$logFile"
    }
}

function Show-AutostartStatus {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Output '开机自启动计划任务：未安装'
    }
    else {
        $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName
        Write-Output "开机自启动计划任务：$($task.State)"
        Write-Output "上次运行：$($taskInfo.LastRunTime)；结果：$($taskInfo.LastTaskResult)"
        Write-Output "下次运行：$($taskInfo.NextRunTime)"
    }
    if (Test-Path -LiteralPath $startupFile) {
        Write-Output "用户登录启动项：已安装（$startupFile）"
    }
    else {
        Write-Output '用户登录启动项：未安装'
    }
    $watchdog = Get-WatchdogProcess
    if ($null -ne $watchdog) {
        Write-Output "无管理员权限守护进程：运行中，PID=$($watchdog.ProcessId)"
    }
    else {
        Write-Output '无管理员权限守护进程：未运行'
    }
    if (Test-Path -LiteralPath $configFile) {
        $config = Get-AutostartConfiguration
        $mysqlReady = Test-TcpPort -HostName $config.MySqlHost -Port $config.MySqlPort
        Write-Output "MySQL：$(if ($mysqlReady) { '运行中' } else { '未运行' })"
    }
    else {
        Write-Output 'MySQL：缺少自启动配置'
    }
    $worker = Get-Module1WorkerProcess
    if ($null -eq $worker) {
        Write-Output '模块1后台运行器：未运行'
    }
    else {
        Write-Output "模块1后台运行器：运行中，PID=$($worker.Id)"
    }
}

switch ($Action) {
    'Install' {
        Install-AutostartTask
    }
    'Uninstall' {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $startupFile -Force -ErrorAction SilentlyContinue
        New-Item -ItemType File -Path $watchdogStopFile -Force | Out-Null
        Write-Output "已卸载自启动入口：$taskName"
    }
    'Run' {
        try {
            Start-AftersalesRuntime
        }
        catch {
            Write-AutostartLog "守护检查失败：$($_.Exception.Message)"
            throw
        }
    }
    'Watch' {
        Start-WatchdogLoop
    }
    'Status' {
        Show-AutostartStatus
    }
}
