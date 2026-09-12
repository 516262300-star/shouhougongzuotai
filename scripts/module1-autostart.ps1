[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Run', 'Watch', 'StartWatch', 'StopWatch', 'Status')]
    [string]$Action = 'Status',
    [string]$MySqlExe,
    [string]$MySqlDefaultsFile,
    [ValidateRange(1, 65535)]
    [int]$MySqlPort = 3306,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 8000,
    [ValidatePattern('^(0\.0\.0\.0|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$')]
    [ValidateScript({ $ip = $null; [System.Net.IPAddress]::TryParse($_, [ref]$ip) })]
    [string]$WebHost = '127.0.0.1',
    [ValidateRange(1, 60)]
    [int]$WatchdogMinutes = 5
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeDir = Join-Path $projectRoot '.runtime'
$configFile = Join-Path $runtimeDir 'module1-autostart.json'
$configBackupFile = Join-Path $runtimeDir 'module1-autostart.backup.json'
$stateFile = Join-Path $runtimeDir 'module1-autostart-status.json'
$mysqlDefaultsBackupFile = Join-Path $runtimeDir 'mysql-defaults-backup.ini'
$mysqlStdoutLog = Join-Path $runtimeDir 'mysql-autostart.log'
$mysqlStderrLog = Join-Path $runtimeDir 'mysql-autostart-error.log'
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
    $probeHost = if ($webHost -eq '0.0.0.0') { '127.0.0.1' } else { $webHost }
    return [pscustomobject]@{
        HostName = $probeHost
        BindAddress = $webHost
        Port = $webPort
        HealthUrl = "http://${probeHost}:$webPort/health/ready"
        RootUrl = "http://${probeHost}:$webPort/"
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

function Read-AutostartConfiguration {
    param([string]$Path)
    $config = Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json
    foreach ($name in @('MySqlExe', 'MySqlDefaultsFile', 'MySqlHost')) {
        if (-not $config.$name) { throw "启动配置缺少字段：$name" }
    }
    if ([int]$config.MySqlPort -lt 1 -or [int]$config.MySqlPort -gt 65535 -or
        [int]$config.WatchdogMinutes -lt 1 -or [int]$config.WatchdogMinutes -gt 60) {
        throw '启动配置端口或守护间隔无效'
    }
    if ($null -ne $config.PSObject.Properties['WebPort'] -and
        ([int]$config.WebPort -lt 1 -or [int]$config.WebPort -gt 65535)) {
        throw '启动配置 Web 端口无效'
    }
    foreach ($pathName in @('MySqlExe', 'MySqlDefaultsFile')) {
        if (-not [System.IO.Path]::IsPathRooted([string]$config.$pathName)) {
            throw "启动配置路径必须是绝对路径：$pathName"
        }
    }
    $identityNames = @('MySqlDataDirectory', 'MySqlDataSourceDirectory', 'MySqlServerUuid')
    $identityCount = @($identityNames | Where-Object { $config.$_ }).Count
    if ($identityCount -ne 0 -and $identityCount -ne 3) { throw '数据库身份配置不完整' }
    return $config
}

function Get-AutostartConfiguration {
    param([switch]$ReadOnly)
    try { return Read-AutostartConfiguration -Path $configFile }
    catch {
        # 只回退到本机安装时保存的有效副本；不生成默认配置、更不创建空库。
        try { $backup = Read-AutostartConfiguration -Path $configBackupFile }
        catch { throw '本机启动配置与恢复副本均不可用，请检查 .runtime 中的配置文件' }
        if ($ReadOnly) { return $backup }
        if (Test-Path -LiteralPath $configFile -PathType Leaf) {
            $invalidCopy = "$configFile.invalid.$([guid]::NewGuid().ToString('N'))"
            Copy-Item -LiteralPath $configFile -Destination $invalidCopy
        }
        Copy-Item -LiteralPath $configBackupFile -Destination $configFile -Force
        Write-AutostartLog '本机启动配置缺失或损坏，已从有效副本恢复；原损坏文件已保留'
        return $backup
    }
}

function Get-MySqlDataDirectory {
    param([string]$DefaultsFile)
    $inServerSection = $false
    $dataPaths = @()
    foreach ($line in (Get-Content -LiteralPath $DefaultsFile -Encoding utf8)) {
        $value = $line.Trim()
        if ($value -match '^\[(.+)\]$') { $inServerSection = $Matches[1] -eq 'mysqld' }
        elseif ($inServerSection -and $value -match '^datadir\s*=\s*(.+)$') {
            $dataPaths += $Matches[1].Trim().Trim('"').Trim("'")
        }
        elseif ($value -match '^!include') { throw '配置包含外部 include，需人工确认数据目录' }
    }
    if ($dataPaths.Count -ne 1 -or -not [System.IO.Path]::IsPathRooted($dataPaths[0])) {
        throw 'MySQL 配置必须明确指定唯一的绝对数据目录，禁止猜测或初始化'
    }
    return [System.IO.Path]::GetFullPath($dataPaths[0]).TrimEnd('\', '/')
}

function Get-MySqlServerUuid {
    param([string]$DataDirectory)
    $identity = Get-Content -LiteralPath (Join-Path $DataDirectory 'auto.cnf') -Raw -Encoding utf8
    if ($identity -notmatch '(?m)^server-uuid\s*=\s*([0-9a-fA-F-]{36})\s*$') {
        throw '原数据目录中没有可核验的 MySQL 身份'
    }
    $uuid = [guid]::Parse($Matches[1]).ToString()
    foreach ($dataFile in @('ibdata1', 'mysql.ibd')) {
        if (-not (Test-Path -LiteralPath (Join-Path $DataDirectory $dataFile) -PathType Leaf)) {
            throw "原数据文件缺失：$dataFile；禁止自动初始化或恢复旧数据覆盖"
        }
    }
    return $uuid
}

function Get-MySqlDataIdentity {
    param([string]$DefaultsFile)
    $dataPath = Get-MySqlDataDirectory -DefaultsFile $DefaultsFile
    $item = Get-Item -LiteralPath $dataPath -Force
    $source = $dataPath
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        if ($item.LinkType -ne 'Junction') { throw '只支持已核实的目录联接，不跟随未知链接' }
        $source = [System.IO.Path]::GetFullPath([string]$item.Target).TrimEnd('\', '/')
    }
    $sourceItem = Get-Item -LiteralPath $source -Force
    if ($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw '原库目标必须为实际目录，不允许多层联接'
    }
    return @{
        MySqlDataDirectory = $dataPath
        MySqlDataSourceDirectory = $source
        MySqlServerUuid = Get-MySqlServerUuid -DataDirectory $source
    }
}

function Restore-MySqlDataDirectory {
    param($Config)
    $dataPath = Get-MySqlDataDirectory -DefaultsFile $Config.MySqlDefaultsFile
    if (-not $Config.MySqlServerUuid) {
        # 兼容未登记身份的旧部署：只能使用已存在的目录，绝不凭空修复。
        Get-MySqlServerUuid -DataDirectory $dataPath | Out-Null
        return
    }
    $pinnedPath = [System.IO.Path]::GetFullPath([string]$Config.MySqlDataDirectory).TrimEnd('\', '/')
    $source = [System.IO.Path]::GetFullPath([string]$Config.MySqlDataSourceDirectory).TrimEnd('\', '/')
    if ($dataPath -ne $pinnedPath) { throw 'MySQL 配置数据目录与登记路径不同，暂停启动' }
    $sourceItem = Get-Item -LiteralPath $source -Force
    if ($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw '登记的原库目标已变为联接，暂停启动'
    }
    if ((Get-MySqlServerUuid -DataDirectory $source) -ne $Config.MySqlServerUuid) {
        throw '原库 UUID 与登记身份不一致，禁止连接另一份数据库'
    }
    $item = Get-Item -LiteralPath $dataPath -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        if ($dataPath -eq $source -or $dataPath -match '[^\x00-\x7F]') {
            throw '只能恢复已登记的英文路径联接，不能创建原库'
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $dataPath) -Force | Out-Null
        New-Item -ItemType Junction -Path $dataPath -Target $source | Out-Null
        Write-AutostartLog '已核验原库身份并恢复缺失的数据目录联接，未移动数据'
    }
    $actual = Get-MySqlDataIdentity -DefaultsFile $Config.MySqlDefaultsFile
    if ($actual.MySqlDataSourceDirectory -ne $source -or $actual.MySqlServerUuid -ne $Config.MySqlServerUuid) {
        throw '现有数据目录指向错误目标，禁止覆盖或自动切库'
    }
}

function Open-AutostartLock {
    param([string]$Name)
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    try {
        return [System.IO.File]::Open((Join-Path $runtimeDir $Name),
            [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None)
    }
    catch [System.IO.IOException] {
        if (($_.Exception.HResult -band 0xffff) -in @(32, 33)) { return $null }
        throw
    }
}

function Write-AutostartState {
    param([string]$Status, [string]$Message = '')
    [ordered]@{
        checked_at = (Get-Date).ToString('o')
        status = $Status
        message = $Message
        checker_pid = $PID
    } | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
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

function Start-WorkbenchMySql {
    param($Config)
    if (-not (Test-Path -LiteralPath $Config.MySqlExe -PathType Leaf)) {
        throw "MySQL 程序不存在：$($Config.MySqlExe)"
    }
    Restore-MySqlDefaultsFile -Config $Config
    Restore-MySqlDataDirectory -Config $Config
    $mysqlArgument = "--defaults-file=`"$($Config.MySqlDefaultsFile)`""
    $lastExitCode = $null
    for ($startAttempt = 1; $startAttempt -le 3; $startAttempt++) {
        if (Test-TcpPort -HostName $Config.MySqlHost -Port $Config.MySqlPort) {
            return
        }
        $process = Start-Process `
            -FilePath $Config.MySqlExe `
            -ArgumentList @($mysqlArgument, '--console') `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $mysqlStdoutLog `
            -RedirectStandardError $mysqlStderrLog `
            -PassThru
        Write-AutostartLog "MySQL 未运行，第 $startAttempt/3 次启动请求已发出，PID=$($process.Id)"
        for ($second = 0; $second -lt 20; $second++) {
            Start-Sleep -Seconds 1
            if (Test-TcpPort -HostName $Config.MySqlHost -Port $Config.MySqlPort) {
                Write-AutostartLog "MySQL 启动成功，PID=$($process.Id)"
                return
            }
            if ($process.HasExited) {
                $lastExitCode = $process.ExitCode
                Write-AutostartLog (
                    "MySQL 第 $startAttempt/3 次启动提前退出，退出码=$lastExitCode；" +
                    "错误日志：$mysqlStderrLog"
                )
                break
            }
        }
        if (-not $process.HasExited) {
            for ($second = 0; $second -lt 40; $second++) {
                Start-Sleep -Seconds 1
                if (Test-TcpPort -HostName $Config.MySqlHost -Port $Config.MySqlPort) {
                    Write-AutostartLog "MySQL 启动成功，PID=$($process.Id)"
                    return
                }
                if ($process.HasExited) {
                    $lastExitCode = $process.ExitCode
                    break
                }
            }
            if (-not $process.HasExited) {
                throw (
                    "MySQL 进程 PID=$($process.Id) 已运行但在 60 秒内未监听 " +
                    "$($Config.MySqlHost):$($Config.MySqlPort)；错误日志：$mysqlStderrLog"
                )
            }
        }
        if ($startAttempt -lt 3) {
            Start-Sleep -Seconds 3
        }
    }
    throw (
        "MySQL 连续 3 次启动失败，最后退出码=$lastExitCode；" +
        "请查看 $mysqlStderrLog"
    )
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

function Get-WorkbenchWebReleaseSource {
    $pointer = Join-Path $runtimeDir 'workbench-web-release.json'
    if (-not (Test-Path -LiteralPath $pointer)) { return $null }
    $release = Get-Content -LiteralPath $pointer -Raw -Encoding utf8 | ConvertFrom-Json
    if (-not $release.source_path) { throw '网页版本缺少 source_path，禁止回退开发代码' }
    $source = (Resolve-Path -LiteralPath (Join-Path $projectRoot $release.source_path)).Path
    $allowed = [System.IO.Path]::GetFullPath((Join-Path $runtimeDir 'releases')) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $source.StartsWith($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw '网页版本必须位于 .runtime/releases 内'
    }
    foreach ($file in @('aftersales_workbench/main.py', 'aftersales_workbench/core/runtime_paths.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $source $file) -PathType Leaf)) {
            throw '网页版本缺少入口或运行路径支持，禁止回退开发代码'
        }
    }
    $index = Join-Path (Split-Path $source -Parent) 'frontend/dist/client/index.html'
    if (-not (Test-Path -LiteralPath $index -PathType Leaf)) { throw '网页版本缺少配套前端产物' }
    return $source
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
    $releaseSource = Get-WorkbenchWebReleaseSource
    if (-not $releaseSource -and -not (Test-Path -LiteralPath $frontendIndex -PathType Leaf)) {
        throw "缺少前端构建产物：$frontendIndex；请先在 frontend 目录执行 npm run build"
    }
    Remove-Item -LiteralPath $webPidFile -Force -ErrorAction SilentlyContinue
    $arguments = @(
        'aftersales_workbench.main:app',
        '--host', $endpoint.BindAddress,
        '--port', [string]$endpoint.Port
    )
    $previousPythonPath = $env:PYTHONPATH
    $previousRuntimeRoot = $env:AFTERSALES_RUNTIME_ROOT
    try {
        $env:AFTERSALES_RUNTIME_ROOT = $projectRoot
        if ($releaseSource) {
            $env:PYTHONPATH = $releaseSource
            $resolved = & (Join-Path $projectRoot '.venv/Scripts/python.exe') -c "import os,pathlib,aftersales_workbench; from aftersales_workbench.core.runtime_paths import get_runtime_root; assert pathlib.Path(aftersales_workbench.__file__).resolve().is_relative_to(pathlib.Path(os.environ['PYTHONPATH']).resolve()); get_runtime_root(); print('web_release_import_ok')"
            if ($LASTEXITCODE -ne 0 -or $resolved -ne 'web_release_import_ok') { throw '网页版本导入验证失败，未启动' }
            $arguments += @('--app-dir', "`"$releaseSource`"")
        }
        $process = Start-Process `
            -FilePath $webExe `
            -ArgumentList $arguments `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $webStdoutLog `
            -RedirectStandardError $webStderrLog `
            -PassThru
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
        $env:AFTERSALES_RUNTIME_ROOT = $previousRuntimeRoot
    }
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
    $cycleLock = Open-AutostartLock -Name 'module1-autostart-cycle.lock'
    if ($null -eq $cycleLock) {
        Write-Output '已有启动检查正在执行，本次不重复启动'
        return
    }
    try {
        $config = Get-AutostartConfiguration
        Write-AutostartState -Status 'checking'
        $mysqlReady = Test-TcpPort -HostName $config.MySqlHost -Port $config.MySqlPort
        if (-not $mysqlReady) { Start-WorkbenchMySql -Config $config }
        $failures = @()
        # Web 与后台各自启动：后台故障不能让用户连排查页面都打不开。
        try { Start-WorkbenchWeb -Config $config | Out-Null }
        catch { $failures += "Web：$($_.Exception.Message)" }
        try {
            Start-WorkbenchWorker
        }
        catch { $failures += "后台：$($_.Exception.Message)" }
        if ($failures.Count) { throw ($failures -join '；') }
        Write-AutostartState -Status 'healthy'
    }
    catch {
        Write-AutostartState -Status 'failed' -Message $_.Exception.Message
        throw
    }
    finally { $cycleLock.Dispose() }
}

function Start-WorkbenchWorker {
    $worker = Get-Module1WorkerProcess
    if ($null -eq $worker) {
        & $workerScript -Action Start | ForEach-Object { Write-AutostartLog $_ }
        $worker = Get-Module1WorkerProcess
        if ($null -eq $worker) {
            throw '售后后台运行器启动后未通过进程核验'
        }
        Write-AutostartLog "售后后台运行器（模块1+模块2+模块3）守护启动成功，PID=$($worker.Id)"
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
    # PID 由持有单例锁的 Watch 自己写入，启动器不得覆盖真正的守护进程 PID。
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        $registered = Get-WatchdogProcess
        if ($null -ne $registered) { return $registered.ProcessId }
        $process.Refresh()
        if ($process.HasExited) {
            throw "模块1守护进程启动失败，请查看 $logFile"
        }
    }
    throw "守护进程15秒内未登记 PID，请检查 $logFile，勿重复启动"
}

function Start-WatchdogLoop {
    $existing = Get-WatchdogProcess
    if ($null -ne $existing -and $existing.ProcessId -ne $PID) { return }
    $watchLock = Open-AutostartLock -Name 'module1-autostart-watch.lock'
    if ($null -eq $watchLock) { return }
    try {
        Set-Content -LiteralPath $watchdogPidFile -Value $PID -Encoding ascii
        Remove-Item -LiteralPath $watchdogStopFile -Force -ErrorAction SilentlyContinue
        Write-AutostartLog "无管理员权限守护进程已启动，PID=$PID"
        while (-not (Test-Path -LiteralPath $watchdogStopFile)) {
            $waitSeconds = 30
            try {
                Start-AftersalesRuntime
                $config = Get-AutostartConfiguration
                $waitSeconds = [int]$config.WatchdogMinutes * 60
            }
            catch {
                Write-AutostartLog "守护检查失败，30秒后重查：$($_.Exception.Message)"
            }
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
        $watchLock.Dispose()
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
    # 身份不明确时先拒绝安装，不能先覆盖旧的有效恢复副本。
    $dataIdentity = Get-MySqlDataIdentity -DefaultsFile $resolvedDefaultsFile
    Copy-Item `
        -LiteralPath $resolvedDefaultsFile `
        -Destination $mysqlDefaultsBackupFile `
        -Force
    $savedConfig = [ordered]@{
        MySqlExe = $resolvedMySqlExe
        MySqlDefaultsFile = $resolvedDefaultsFile
        MySqlDefaultsBackupFile = $mysqlDefaultsBackupFile
        MySqlHost = '127.0.0.1'
        MySqlPort = $MySqlPort
        WebHost = $WebHost
        WebPort = $WebPort
        WatchdogMinutes = $WatchdogMinutes
        InstalledAt = (Get-Date).ToString('s')
        InstalledBy = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    }
    foreach ($key in $dataIdentity.Keys) { $savedConfig[$key] = $dataIdentity[$key] }
    $savedConfig | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding utf8
    Copy-Item -LiteralPath $configFile -Destination $configBackupFile -Force

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
    if (Test-Path -LiteralPath $stateFile) {
        try {
            $state = Get-Content -LiteralPath $stateFile -Raw -Encoding utf8 | ConvertFrom-Json
            Write-Output "最近启动检查：$($state.checked_at)，$($state.status) $($state.message)"
        }
        catch { Write-Output '最近启动检查状态暂时不可读取' }
    }
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
        $config = Get-AutostartConfiguration -ReadOnly
        $mysqlReady = Test-TcpPort -HostName $config.MySqlHost -Port $config.MySqlPort
        Write-Output "MySQL：$(if ($mysqlReady) { '运行中' } else { '未运行' })"
    }
    else {
        Write-Output 'MySQL：缺少自启动配置'
    }
    $worker = Get-Module1WorkerProcess
    if ($null -eq $worker) {
        Write-Output '售后后台运行器（模块1+模块2+模块3）：未运行'
    }
    else {
        Write-Output "售后后台运行器（模块1+模块2+模块3）：运行中，PID=$($worker.Id)"
    }
    if (Test-Path -LiteralPath $configFile) {
        $endpoint = Get-WorkbenchWebEndpoint -Config (Get-AutostartConfiguration -ReadOnly)
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
    'StartWatch' {
        Start-WatchdogProcess | Out-Null
    }
    'StopWatch' {
        New-Item -ItemType File -Path $watchdogStopFile -Force | Out-Null
        Write-Output '已请求守护进程自然退出，不停止数据库、网页或业务后台'
    }
    'Status' {
        Show-AutostartStatus
    }
}
