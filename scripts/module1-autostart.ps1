[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Run', 'Watch', 'Status')]
    [string]$Action = 'Status',
    [string]$MySqlExe,
    [string]$MySqlDefaultsFile,
    [ValidateRange(1, 65535)]
    [int]$MySqlPort = 3306,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 8000,
    [ValidateRange(1, 60)]
    [int]$WatchdogMinutes = 5
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeDir = Join-Path $projectRoot '.runtime'
$configFile = Join-Path $runtimeDir 'module1-autostart.json'
$mysqlDefaultsBackupFile = Join-Path $runtimeDir 'mysql-defaults-backup.ini'
$logFile = Join-Path $runtimeDir 'module1-autostart.log'
$workerScript = Join-Path $PSScriptRoot 'module1-worker.ps1'
$webPidFile = Join-Path $runtimeDir 'workbench-web.pid'
$webStdoutLog = Join-Path $runtimeDir 'workbench-web.log'
$webStderrLog = Join-Path $runtimeDir 'workbench-web-error.log'
$webExe = Join-Path $projectRoot '.venv\Scripts\uvicorn.exe'
$frontendIndex = Join-Path $projectRoot 'frontend\dist\client\index.html'
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

function Get-WorkbenchWebEndpoint {
    param($Config)
    $webHost = '127.0.0.1'
    $webPort = 8000
    if ($null -ne $Config.PSObject.Properties['WebHost'] -and $Config.WebHost) {
        $webHost = [string]$Config.WebHost
    }
    if ($null -ne $Config.PSObject.Properties['WebPort'] -and $Config.WebPort) {
        $webPort = [int]$Config.WebPort
    }
    return [pscustomobject]@{
        HostName = $webHost
        Port = $webPort
        HealthUrl = "http://${webHost}:$webPort/health/ready"
        RootUrl = "http://${webHost}:$webPort/"
    }
}

function Test-WorkbenchWebHealth {
    param($Endpoint)
    try {
        $response = Invoke-WebRequest `
            -Uri $Endpoint.HealthUrl `
            -UseBasicParsing `
            -TimeoutSec 3
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
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
    # 安装动作可能由 PowerShell 7 执行，其 UTF-8 输出默认不带 BOM。
    # Windows PowerShell 5.1 守护进程若按系统代码页读取，中文项目路径会
    # 乱码并让 JSON 中的反斜杠转义失效，因此读取时必须显式指定 UTF-8。
    return Get-Content -LiteralPath $configFile -Raw -Encoding utf8 | ConvertFrom-Json
}

function Restore-MySqlDefaultsFile {
    param($Config)
    $defaultsFile = [string]$Config.MySqlDefaultsFile
    if (Test-Path -LiteralPath $defaultsFile -PathType Leaf) {
        return
    }
    $backupFile = $mysqlDefaultsBackupFile
    if (
        $null -ne $Config.PSObject.Properties['MySqlDefaultsBackupFile'] -and
        $Config.MySqlDefaultsBackupFile
    ) {
        $backupFile = [string]$Config.MySqlDefaultsBackupFile
    }
    if (-not (Test-Path -LiteralPath $backupFile -PathType Leaf)) {
        throw (
            "MySQL 配置文件不存在：$defaultsFile；本地恢复副本也不存在：$backupFile；" +
            '请在 MySQL 运行后重新执行 Install'
        )
    }
    $defaultsDirectory = Split-Path -Parent $defaultsFile
    if (-not (Test-Path -LiteralPath $defaultsDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $defaultsDirectory -Force | Out-Null
    }
    Copy-Item -LiteralPath $backupFile -Destination $defaultsFile -Force
    if (-not (Test-Path -LiteralPath $defaultsFile -PathType Leaf)) {
        throw "MySQL 配置文件自动恢复失败：$defaultsFile"
    }
    Write-AutostartLog "MySQL 配置文件缺失，已从本地副本自动恢复：$defaultsFile"
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

function Get-WorkbenchWebProcess {
    if (-not (Test-Path -LiteralPath $webPidFile)) {
        return $null
    }
    $webPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $webPidFile -Raw).Trim(), [ref]$webPid)) {
        return $null
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$webPid" -ErrorAction SilentlyContinue
    if ($null -eq $processInfo) {
        return $null
    }
    $commandLine = [string]$processInfo.CommandLine
    if (
        $processInfo.Name -notin @('python.exe', 'pythonw.exe', 'uvicorn.exe') -or
        -not $commandLine.Contains('aftersales_workbench.main:app') -or
        -not $commandLine.Contains($projectRoot)
    ) {
        return $null
    }
    return Get-Process -Id $webPid -ErrorAction SilentlyContinue
}

function Get-WorkbenchWebListenerProcess {
    param($Endpoint)
    $listener = Get-NetTCPConnection `
        -State Listen `
        -LocalPort $Endpoint.Port `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @($Endpoint.HostName, '0.0.0.0', '::') } |
        Select-Object -First 1
    if ($null -eq $listener) {
        return $null
    }
    return Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
}

function Start-WorkbenchWeb {
    param($Config)
    $endpoint = Get-WorkbenchWebEndpoint -Config $Config
    if (Test-WorkbenchWebHealth -Endpoint $endpoint) {
        $running = Get-WorkbenchWebListenerProcess -Endpoint $endpoint
        if ($null -ne $running) {
            Set-Content -LiteralPath $webPidFile -Value $running.Id -Encoding ascii
        }
        return $running
    }
    if (Test-TcpPort -HostName $endpoint.HostName -Port $endpoint.Port) {
        throw "工作台端口 $($endpoint.HostName):$($endpoint.Port) 已被占用，但健康检查失败"
    }
    $existing = Get-WorkbenchWebProcess
    if ($null -ne $existing) {
        throw "工作台 Web 进程存在但健康检查失败，PID=$($existing.Id)，请查看 $webStderrLog"
    }
    if (-not (Test-Path -LiteralPath $webExe -PathType Leaf)) {
        throw "缺少工作台 Web 入口：$webExe"
    }
    if (-not (Test-Path -LiteralPath $frontendIndex -PathType Leaf)) {
        throw "缺少前端构建产物：$frontendIndex；请先在 frontend 目录执行 npm run build"
    }
    Remove-Item -LiteralPath $webPidFile -Force -ErrorAction SilentlyContinue
    $arguments = @(
        'aftersales_workbench.main:app',
        '--host', $endpoint.HostName,
        '--port', [string]$endpoint.Port
    )
    $process = Start-Process `
        -FilePath $webExe `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $webStdoutLog `
        -RedirectStandardError $webStderrLog `
        -PassThru
    Write-AutostartLog "工作台 Web 未运行，已发出隐藏启动请求，启动器 PID=$($process.Id)"
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-WorkbenchWebHealth -Endpoint $endpoint) {
            $running = Get-WorkbenchWebListenerProcess -Endpoint $endpoint
            if ($null -eq $running) {
                continue
            }
            Set-Content -LiteralPath $webPidFile -Value $running.Id -Encoding ascii
            Write-AutostartLog "工作台 Web 守护启动成功：$($endpoint.RootUrl)，PID=$($running.Id)"
            return $running
        }
    }
    Remove-Item -LiteralPath $webPidFile -Force -ErrorAction SilentlyContinue
    throw "工作台 Web 在 60 秒内未通过健康检查，请查看 $webStderrLog"
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
        Restore-MySqlDefaultsFile -Config $config
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
            throw '售后后台运行器启动后未通过进程核验'
        }
        Write-AutostartLog "售后后台运行器（模块1+模块3）守护启动成功，PID=$($worker.Id)"
    }

    Start-WorkbenchWeb -Config $config | Out-Null
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
    Copy-Item `
        -LiteralPath $resolvedDefaultsFile `
        -Destination $mysqlDefaultsBackupFile `
        -Force
    [ordered]@{
        MySqlExe = $resolvedMySqlExe
        MySqlDefaultsFile = $resolvedDefaultsFile
        MySqlDefaultsBackupFile = $mysqlDefaultsBackupFile
        MySqlHost = '127.0.0.1'
        MySqlPort = $MySqlPort
        WebHost = '127.0.0.1'
        WebPort = $WebPort
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
        -Description '登录后启动并每 5 分钟守护利德仕售后工作台 MySQL、后台运行器与 Web 服务'
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
        $shortcut.Description = '利德仕售后工作台登录自启动与5分钟守护'
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
        Write-Output '售后后台运行器（模块1+模块3）：未运行'
    }
    else {
        Write-Output "售后后台运行器（模块1+模块3）：运行中，PID=$($worker.Id)"
    }
    if (Test-Path -LiteralPath $configFile) {
        $endpoint = Get-WorkbenchWebEndpoint -Config (Get-AutostartConfiguration)
        $webProcess = Get-WorkbenchWebProcess
        $webHealthy = Test-WorkbenchWebHealth -Endpoint $endpoint
        if ($webHealthy -and $null -ne $webProcess) {
            Write-Output "售后工作台 Web：运行中，PID=$($webProcess.Id)，地址=$($endpoint.RootUrl)"
        }
        elseif ($webHealthy) {
            Write-Output "售后工作台 Web：端口已有健康服务，地址=$($endpoint.RootUrl)"
        }
        else {
            Write-Output "售后工作台 Web：未运行，地址=$($endpoint.RootUrl)"
        }
    }
    else {
        Write-Output '售后工作台 Web：缺少自启动配置'
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
