[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$RemoteHost,
    [Parameter(Mandatory)][string]$RemoteBackupRoot,
    [Parameter(Mandatory)][string]$KeyFile,
    [Parameter(Mandatory)][string]$KnownHostsFile,
    [Parameter(Mandatory)][string]$Destination
)
$ErrorActionPreference = 'Stop'
if (-not $PSCmdlet.ShouldProcess($Destination, 'Copy latest verified office backup over pinned SSH')) { return }
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$destinationRoot = (Resolve-Path -LiteralPath $Destination).Path
$statusFile = Join-Path $destinationRoot 'pull-status.json'
$result = [ordered]@{checked_at=(Get-Date -Format o);status='checking';backup=$null;error=$null}
$lock = $null
try {
    if ($RemoteHost -notmatch '^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+$') { throw 'Invalid remote host' }
    if ($RemoteBackupRoot -notmatch '^[A-Za-z]:[/\\][A-Za-z0-9_/\\-]+$') { throw 'Invalid remote backup root' }
    $lock = [IO.File]::Open((Join-Path $destinationRoot 'pull.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $sshArgs = @('-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','-o',"UserKnownHostsFile=$KnownHostsFile",'-i',$KeyFile)
    $query = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(`$false); Get-Content -LiteralPath '$RemoteBackupRoot/latest.json' -Raw -Encoding utf8"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($query))
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'  # Windows PowerShell treats native stderr as an error record.
        $raw = & ssh @sshArgs $RemoteHost powershell.exe -NoProfile -NonInteractive -EncodedCommand $encoded 2> (Join-Path $destinationRoot 'connection-error.log')
        $sshExit = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $oldPreference }
    if ($sshExit -ne 0) {
        $result.status = 'deferred'
        $result.error = 'Office not reachable or no published backup; retry on next scheduled run'
        return
    }
    $latest = ($raw -join "`n") | ConvertFrom-Json
    $remotePath = ([string]$latest.path).Replace('\','/')
    $expectedRoot = $RemoteBackupRoot.Replace('\','/').TrimEnd('/')
    $name = ($remotePath -split '/')[-1]
    if ($name -notmatch '^daily-\d{8}-\d{6}$' -or $remotePath -ne "$expectedRoot/$name") { throw 'Unexpected remote backup path' }
    $target = Join-Path $destinationRoot $name
    $result.backup = $name
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $verify = Join-Path $PSScriptRoot 'office_verified_backup.py'
    if (-not (Test-Path -LiteralPath $target)) {
        $stage = Join-Path $destinationRoot ($name + '.partial-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $stage | Out-Null
        & scp -q -r @sshArgs "${RemoteHost}:$remotePath" $stage
        if ($LASTEXITCODE -ne 0) { throw 'Backup download incomplete; partial directory retained' }
        $download = Join-Path $stage $name
        if (-not (Test-Path -LiteralPath (Join-Path $download 'backup-complete.json'))) { throw 'Remote backup incomplete' }
        & $python $verify --output $download --verify-only
        if ($LASTEXITCODE -ne 0) { throw 'Downloaded backup verification failed' }
        # Both paths are fixed children of the resolved protected destination.
        if (-not ([IO.Path]::GetFullPath($download)).StartsWith($destinationRoot + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid staging path' }
        Move-Item -LiteralPath $download -Destination $target
        Remove-Item -LiteralPath $stage  # Empty directory only; never recursive.
    }
    else {
        & $python $verify --output $target --verify-only
        if ($LASTEXITCODE -ne 0) { throw 'Existing local backup verification failed' }
    }
    $result.status = 'completed'
}
catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
    throw
}
finally {
    $result | ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding utf8
    if ($null -ne $lock) { $lock.Dispose() }
}
