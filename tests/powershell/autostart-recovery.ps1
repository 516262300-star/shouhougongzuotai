param([string]$SourceFile, [string]$TestRoot, [string]$Case)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SourceFile, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Load only function definitions. Never execute the production script's action switch.
foreach ($function in $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)) { Invoke-Expression $function.Extent.Text }
$runtimeDir = $TestRoot
$configFile = Join-Path $TestRoot 'config.json'
$configBackupFile = Join-Path $TestRoot 'config-backup.json'
$mysqlDefaultsBackupFile = Join-Path $TestRoot 'defaults-backup.ini'
$stateFile = Join-Path $TestRoot 'state.json'
$watchdogPidFile = Join-Path $TestRoot 'watch.pid'
$watchdogStopFile = Join-Path $TestRoot 'watch.stop'
$defaultsFile = Join-Path $TestRoot 'my.ini'
$dataSource = Join-Path $TestRoot 'original'
$aliasPath = Join-Path $TestRoot 'alias'
New-Item -ItemType Directory -Path $dataSource | Out-Null
$uuid = '11111111-2222-3333-4444-555555555555'
@('[auto]', "server-uuid=$uuid") | Set-Content -LiteralPath (Join-Path $dataSource 'auto.cnf')
foreach ($name in @('ibdata1', 'mysql.ibd')) {
    'fake fixture, not a database' | Set-Content -LiteralPath (Join-Path $dataSource $name)
}
@('[mysqld]', "datadir=$aliasPath") | Set-Content -LiteralPath $defaultsFile -Encoding utf8
$config = [pscustomobject]@{
    MySqlExe = 'C:/fake/mysqld.exe'; MySqlDefaultsFile = $defaultsFile
    MySqlHost = '127.0.0.1'; MySqlPort = 3306; WatchdogMinutes = 5
    MySqlDataDirectory = $aliasPath; MySqlDataSourceDirectory = $dataSource
    MySqlServerUuid = $uuid
}
$config | ConvertTo-Json | Set-Content -LiteralPath $configBackupFile -Encoding utf8
$script:logMessages = @()
function Write-AutostartLog { param([string]$Message) $script:logMessages += $Message }
function Assert-True { param($Value, [string]$Message) if (-not $Value) { throw $Message } }
function Assert-Throws {
    param([scriptblock]$Action)
    $thrown = $false
    try { & $Action | Out-Null } catch { $thrown = $true }
    Assert-True $thrown 'Expected a safe refusal'
}
switch ($Case) {
    'web_lan_bind' {
        $local = Get-WorkbenchWebEndpoint -Config $config
        Assert-True ($local.BindAddress -eq '127.0.0.1') 'Default must remain loopback'
        $config | Add-Member -NotePropertyName WebHost -NotePropertyValue '0.0.0.0'
        $lan = Get-WorkbenchWebEndpoint -Config $config
        Assert-True ($lan.BindAddress -eq '0.0.0.0') 'LAN bind lost'
        Assert-True ($lan.HostName -eq '127.0.0.1') 'Health probe must use a connectable address'
        Assert-True ($lan.HealthUrl -eq 'http://127.0.0.1:8000/health/ready') 'Wildcard must not enter health URL'
    }
    'config_missing' {
        $actual = Get-AutostartConfiguration
        Assert-True ($actual.MySqlServerUuid -eq $uuid) 'Incorrect restored identity'
        Assert-True (Test-Path -LiteralPath $configFile) 'Missing restored config'
    }
    'config_readonly' {
        Get-AutostartConfiguration -ReadOnly | Out-Null
        Assert-True (-not (Test-Path -LiteralPath $configFile)) 'Read-only check must not restore files'
    }
    'config_invalid_web_port' {
        $config | Add-Member -NotePropertyName WebPort -NotePropertyValue 70000
        $config | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding utf8
        Assert-Throws { Read-AutostartConfiguration -Path $configFile }
    }
    'config_corrupt' {
        'broken json' | Set-Content -LiteralPath $configFile
        Get-AutostartConfiguration | Out-Null
        $saved = @(Get-ChildItem -LiteralPath $TestRoot -Filter 'config.json.invalid.*')
        Assert-True ($saved.Count -eq 1) 'Original corrupt config not preserved'
        Assert-True ((Get-Content -LiteralPath $saved[0].FullName -Raw).Trim() -eq 'broken json') 'Corrupt copy changed'
    }
    'config_both_invalid' {
        'broken' | Set-Content -LiteralPath $configBackupFile
        Assert-Throws { Get-AutostartConfiguration }
        Assert-True (-not (Test-Path -LiteralPath $configFile)) 'Must not generate defaults'
    }
    'identity_partial' {
        $config.MySqlServerUuid = $null
        $config | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding utf8
        Assert-Throws { Read-AutostartConfiguration -Path $configFile }
    }
    'defaults_missing' {
        Copy-Item -LiteralPath $defaultsFile -Destination $mysqlDefaultsBackupFile
        $config.MySqlDefaultsFile = Join-Path $TestRoot 'restored.ini'
        Restore-MySqlDefaultsFile -Config $config
        Assert-True ((Get-MySqlDataDirectory $config.MySqlDefaultsFile) -eq $aliasPath) 'Wrong defaults restored'
    }
    'alias_missing' {
        Restore-MySqlDataDirectory -Config $config
        Assert-True ((Get-Item -LiteralPath $aliasPath).LinkType -eq 'Junction') 'Missing junction'
        Assert-True ((Get-Item -LiteralPath $aliasPath).Target -eq $dataSource) 'Wrong target'
    }
    'alias_correct' {
        New-Item -ItemType Junction -Path $aliasPath -Target $dataSource | Out-Null
        Restore-MySqlDataDirectory -Config $config
        Assert-True ($script:logMessages.Count -eq 0) 'Must not recreate a correct junction'
    }
    'alias_wrong_target' {
        $other = Join-Path $TestRoot 'other'
        Copy-Item -LiteralPath $dataSource -Destination $other -Recurse
        New-Item -ItemType Junction -Path $aliasPath -Target $other | Out-Null
        Assert-Throws { Restore-MySqlDataDirectory -Config $config }
        Assert-True ((Get-Item -LiteralPath $aliasPath).Target -eq $other) 'Wrong link must not be overwritten'
    }
    'uuid_mismatch' {
        $config.MySqlServerUuid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        Assert-Throws { Restore-MySqlDataDirectory -Config $config }
        Assert-True (-not (Test-Path -LiteralPath $aliasPath)) 'Must not create link for wrong identity'
    }
    'source_missing' {
        $config.MySqlDataSourceDirectory = Join-Path $TestRoot 'missing'
        Assert-Throws { Restore-MySqlDataDirectory -Config $config }
        Assert-True (-not (Test-Path -LiteralPath $config.MySqlDataSourceDirectory)) 'Must not create a database'
    }
    'core_file_missing' {
        Remove-Item -LiteralPath (Join-Path $dataSource 'mysql.ibd')
        Assert-Throws { Restore-MySqlDataDirectory -Config $config }
    }
    'datadir_changed' {
        @('[mysqld]', "datadir=$(Join-Path $TestRoot 'different')") | Set-Content -LiteralPath $defaultsFile -Encoding utf8
        Assert-Throws { Restore-MySqlDataDirectory -Config $config }
    }
    'legacy_missing' {
        $config.MySqlServerUuid = $null
        Assert-Throws { Restore-MySqlDataDirectory -Config $config }
    }
    'cycle_lock' {
        $lock = Open-AutostartLock -Name 'module1-autostart-cycle.lock'
        try {
            $second = Open-AutostartLock -Name 'module1-autostart-cycle.lock'
            Assert-True ($null -eq $second) 'Duplicate lock must not be acquired'
            Start-AftersalesRuntime
            Assert-True (-not (Test-Path -LiteralPath $stateFile)) 'Duplicate cycle must not modify state'
        } finally { $lock.Dispose() }
        $next = Open-AutostartLock -Name 'module1-autostart-cycle.lock'
        Assert-True ($null -ne $next) 'Lock must recover after release'
        $next.Dispose()
    }
    'cold_start_order' {
        $script:sequence = @()
        function Test-TcpPort { return $false }
        function Start-WorkbenchMySql { param($Config) $script:sequence += 'mysql' }
        function Start-WorkbenchWeb { param($Config) $script:sequence += 'web' }
        function Start-WorkbenchWorker { $script:sequence += 'worker' }
        Start-AftersalesRuntime
        Assert-True (($script:sequence -join ',') -eq 'mysql,web,worker') 'Wrong cold startup order'
        $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        Assert-True ($state.status -eq 'healthy') 'Successful cold startup must be healthy'
    }
    { $_ -in @('web_before_worker', 'web_failure_keeps_worker') } {
        $script:sequence = @()
        function Test-TcpPort { return $true }
        function Start-WorkbenchWeb {
            param($Config)
            $script:sequence += 'web'
            if ($Case -eq 'web_failure_keeps_worker') { throw 'simulated web failure' }
        }
        function Start-WorkbenchWorker {
            $script:sequence += 'worker'
            if ($Case -eq 'web_before_worker') { throw 'simulated worker failure' }
        }
        Assert-Throws { Start-AftersalesRuntime }
        Assert-True (($script:sequence -join ',') -eq 'web,worker') 'Both services must be tried, web first'
        $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        Assert-True ($state.status -eq 'failed') 'Failure must be recorded'
    }
    'watch_bad_config_retries' {
        function Get-WatchdogProcess { return $null }
        function Get-AutostartConfiguration { throw 'simulated invalid config' }
        function Start-Sleep {
            param($Seconds)
            New-Item -ItemType File -Path $watchdogStopFile | Out-Null
        }
        Start-WatchdogLoop
        Assert-True (@($script:logMessages | Where-Object { $_ -match '30' -and $_ -match 'simulated invalid config' }).Count -eq 1) 'Watch must catch startup errors and schedule retry'
        Assert-True (-not (Test-Path -LiteralPath $watchdogPidFile)) 'Watch PID cleanup failed'
    }
    'watch_singleton' {
        function Get-WatchdogProcess { return $null }
        $lock = Open-AutostartLock -Name 'module1-autostart-watch.lock'
        try {
            Start-WatchdogLoop
            Assert-True (-not (Test-Path -LiteralPath $watchdogPidFile)) 'Second watcher must not overwrite PID'
        } finally { $lock.Dispose() }
    }
    default { throw "Unknown test case: $Case" }
}
Write-Output "PASS $Case"
