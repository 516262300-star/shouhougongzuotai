[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$BackupRoot,
    [Parameter(Mandatory)][string]$MySqlDump,
    [Parameter(Mandatory)][string]$AdminClientFile,
    [string]$WatchdogTask = 'Leedis Aftersales Module1 Watchdog',
    [ValidateRange(30,1800)][int]$WaitSeconds = 900
)
$ErrorActionPreference = 'Stop'
if (-not $PSCmdlet.ShouldProcess($BackupRoot, 'Pause worker safely, back up and verify, then resume watchdog')) { return }
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtime = Join-Path $root '.runtime'
$statusPath = Join-Path $runtime 'office-backup-status.json'
$stopPath = Join-Path $runtime 'module1-worker.stop'
$cycleLock = $null
$ownsStop = $false
$result = [ordered]@{started_at=(Get-Date -Format o);status='running';output=$null;error=$null}
try {
    $backupBase = (Resolve-Path -LiteralPath $BackupRoot).Path
    $drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($backupBase).Substring(0,1))
    if ($drive.Free -lt 20GB) { throw 'Backup volume has less than 20 GB free' }
    if (-not (Get-ScheduledTask -TaskName $WatchdogTask).Settings.Enabled) { throw 'Watchdog is disabled; maintenance may already be in progress' }
    $deadline = (Get-Date).AddSeconds(90)
    do {
        try { $cycleLock = [IO.File]::Open((Join-Path $runtime 'module1-autostart-cycle.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
        catch [IO.IOException] { Start-Sleep -Seconds 2 }
    } while ($null -eq $cycleLock -and (Get-Date) -lt $deadline)
    if ($null -eq $cycleLock) { throw 'Watchdog lock busy; backup skipped' }
    if (Test-Path -LiteralPath $stopPath) { throw 'Existing worker stop request; backup skipped' }
    # Track the entire worker tree so a launcher exiting early cannot hide a writer.
    $all = @(Get-CimInstance Win32_Process)
    $entry = Join-Path $root '.venv\Scripts\aftersales-run-module1.exe'
    $ids = @($all | Where-Object { $_.ExecutablePath -eq $entry } | ForEach-Object { [int]$_.ProcessId })
    do {
        $children = @($all | Where-Object { $_.ParentProcessId -in $ids -and $_.ProcessId -notin $ids } | ForEach-Object { [int]$_.ProcessId })
        $ids += $children
    } while ($children.Count -gt 0)
    $tracked = @($all | Where-Object { $_.ProcessId -in $ids })
    New-Item -ItemType File -Path $stopPath -ErrorAction Stop | Out-Null
    $ownsStop = $true
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        $alive = @(Get-CimInstance Win32_Process | Where-Object {
            $current = $_
            @($tracked | Where-Object { $_.ProcessId -eq $current.ProcessId -and $_.CreationDate -eq $current.CreationDate }).Count -gt 0
        })
        if ($alive.Count -eq 0) { break }
        if ((Get-Date) -ge $deadline) { throw 'Worker still busy; no process killed and no backup created' }
        Start-Sleep -Seconds 3
    } while ($true)
    $output = Join-Path $backupBase ('daily-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $result.output = $output
    $python = Join-Path $root '.venv\Scripts\python.exe'
    & $python (Join-Path $PSScriptRoot 'office_verified_backup.py') --root $root --output $output --mysqldump $MySqlDump --admin-client-file $AdminClientFile
    if ($LASTEXITCODE -ne 0) { throw 'Backup verification failed; inspect protected task log' }
    if (-not (Test-Path -LiteralPath (Join-Path $output 'backup-complete.json'))) { throw 'Backup completion marker missing' }
    # Publish latest only after all verification succeeds; failed runs preserve it.
    $latest = Join-Path $backupBase 'latest.json'
    $temporary = $latest + '.tmp'
    @{path=$output;completed_at=(Get-Date -Format o)} | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $latest -Force
    $result.status = 'completed'
}
catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
    throw
}
finally {
    if ($ownsStop) { Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue }
    if ($null -ne $cycleLock) { $cycleLock.Dispose() }
    $result.finished_at = Get-Date -Format o
    $result | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding utf8
    if ($ownsStop) { Start-ScheduledTask -TaskName $WatchdogTask }
}
