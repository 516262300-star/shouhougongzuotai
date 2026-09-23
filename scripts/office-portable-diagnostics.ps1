#Requires -Version 5.1
[CmdletBinding()]
param([string]$OutputDirectory)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
if (-not $OutputDirectory) { $OutputDirectory=$PSScriptRoot }
$destination=(Resolve-Path -LiteralPath $OutputDirectory).Path
$root=Join-Path $env:ProgramData 'LdsAftersalesPortableAccess'
$report=[Collections.Generic.List[string]]::new()
$report.Add('LDS temporary connection diagnostics - '+(Get-Date -Format o))
$report.Add('Computer: '+$env:COMPUTERNAME+'; User: '+$env:USERNAME)
$checks=[ordered]@{
    Network={
        Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength,AddressState
        Get-NetAdapter | Select-Object Name,Status,LinkSpeed
    }
    Listener={
        Get-NetTCPConnection -LocalPort 22222 -ErrorAction SilentlyContinue |
            Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,State,OwningProcess
    }
    State={
        if(Test-Path -LiteralPath (Join-Path $root 'state.json')) {
            Get-Content -LiteralPath (Join-Path $root 'state.json') -Raw -Encoding UTF8 |
                ConvertFrom-Json | Select-Object owner,login_name,development_address,binary,phase,process_id,process_start_ticks,expires_at,rule_name
        }
        [pscustomobject]@{stop_requested=(Test-Path -LiteralPath (Join-Path $root 'stop.request'))}
    }
    Processes={
        Get-CimInstance Win32_Process | Where-Object {
            $_.Name -in @('sshd.exe','sshd-session.exe','sftp-server.exe') -or
            ($_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*office-portable-access.ps1*')
        } | Select-Object Name,ProcessId,ParentProcessId,CreationDate,ExecutablePath
    }
    Firewall={
        $rules=@(Get-NetFirewallRule -Name 'LDS-Aftersales-Portable-*' -ErrorAction SilentlyContinue)
        [pscustomobject]@{managed_rule_count=$rules.Count}
        foreach($rule in $rules){
            $a=$rule|Get-NetFirewallAddressFilter
            $p=$rule|Get-NetFirewallPortFilter
            $app=$rule|Get-NetFirewallApplicationFilter
            [pscustomobject]@{name=$rule.Name;enabled=$rule.Enabled;action=$rule.Action;direction=$rule.Direction;source=($a.RemoteAddress -join ',');port=($p.LocalPort -join ',');program=$app.Program}
        }
    }
    Logs={
        $logs=Get-ChildItem -LiteralPath $root -Filter 'connection-*.err.log' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 4
        foreach($log in $logs){
            $log.Name+'; Updated: '+$log.LastWriteTime
            # Exclude executed command text, environment, key bodies and business data.
            Get-Content -LiteralPath $log.FullName -Tail 80 | Where-Object {
                $_ -match 'fatal|error|listen|disconnect|subsystem|exit|bind|permission|refused|timed out' -and
                $_ -notmatch 'EncodedCommand|exec command|command:|environment'
            } | Select-Object -Last 18
        }
    }
    Power={
        & powercfg.exe /getactivescheme
        & powercfg.exe /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
    }
}
foreach($name in $checks.Keys){
    $report.Add("`r`n[$name]")
    try {
        $value=& $checks[$name]
        if($null -eq $value){$report.Add('(no records)')}else{$report.Add(($value|Format-List|Out-String -Width 240))}
    }catch{$report.Add('Read failed: '+$_.Exception.Message)}
}
$path=Join-Path $destination ('connection-diagnostics-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.txt')
[IO.File]::WriteAllLines($path,$report,[Text.UTF8Encoding]::new($true))
Write-Host 'Diagnostics saved (no settings were changed):' -ForegroundColor Green
Write-Host $path
Write-Host 'Send this TXT file back. Do not send any private keys or passwords.'
