#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Start','Stop')][string]$Action = 'Start',
    [string]$DevelopmentAddress,
    [string]$PublicKeyFile,
    [string]$Archive,
    [ValidateRange(1,60)][int]$Minutes = 60
)

# Temporary bootstrap only. No Windows capability, service, or business-state changes.
$ErrorActionPreference = 'Stop'
if (-not $PublicKeyFile) { $PublicKeyFile = Join-Path $PSScriptRoot 'deployment-key.pub' }
if (-not $Archive) { $Archive = Join-Path $PSScriptRoot 'OpenSSH-Win64.zip' }
$expectedHash = '23f50f3458c4c5d0b12217c6a5ddfde0137210a30fa870e98b29827f7b43aba5'
$ownerTag = 'LDS-Portable-Deployment-v1'
$root = Join-Path $env:ProgramData 'LdsAftersalesPortableAccess'
$stateFile = Join-Path $root 'state.json'
$stopFile = Join-Path $root 'stop.request'
$portNumber = 22222

function Get-ContentHash([string]$Path) {
    # Windows PowerShell 5.1 Get-FileHash can inherit WhatIf and return no hash.
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $stream = [IO.File]::OpenRead($resolved)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $hasher.Dispose() }
}

function Protect-Path([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer) {
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    } else {
        $acl = [Security.AccessControl.FileSecurity]::new()
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetAccessRuleProtection($true,$false)
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $acl.SetOwner($admins)
    foreach ($sid in @($admins,[Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl',$inheritance,'None','Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Save-State {
    $state | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
}

function Disable-WindowQuickEdit {
    # Only this console window; do not change the user's registry/defaults.
    if (-not ('LdsPortableConsoleInput' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class LdsPortableConsoleInput {
    [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int id);
    [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr h, out uint mode);
    [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr h, uint mode);
    public static bool DisableQuickEdit() {
        IntPtr h = GetStdHandle(-10);
        uint mode;
        if (!GetConsoleMode(h, out mode)) return false; // Redirected input has no console.
        return SetConsoleMode(h, (mode | 0x80U) & ~0x40U);
    }
}
'@
    }
    $null = [LdsPortableConsoleInput]::DisableQuickEdit()
}

function Remove-OwnRule($State) {
    if (-not $State.rule_name) { return }
    if ($State.rule_name -notmatch '^LDS-Aftersales-Portable-[a-f0-9]{32}$') { throw 'Invalid rule identity' }
    $rule = Get-NetFirewallRule -Name $State.rule_name -ErrorAction SilentlyContinue
    if (-not $rule) { return }
    $address = $rule | Get-NetFirewallAddressFilter
    $port = $rule | Get-NetFirewallPortFilter
    $app = $rule | Get-NetFirewallApplicationFilter
    if ($rule.Description -ne $ownerTag -or $rule.Direction -ne 'Inbound' -or $rule.Action -ne 'Allow' -or
        @($address.RemoteAddress).Count -ne 1 -or $address.RemoteAddress -ne $State.development_address -or
        @($port.LocalPort).Count -ne 1 -or $port.LocalPort -ne [string]$portNumber -or
        [string]$port.Protocol -notin @('TCP','6') -or $app.Program -ine $State.binary) {
        throw 'Firewall rule changed; refusing to modify it'
    }
    $rule | Remove-NetFirewallRule
}

if ($Action -eq 'Start') {
    $ip = $null
    if (-not [Net.IPAddress]::TryParse($DevelopmentAddress,[ref]$ip) -or
        $ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $ip.ToString() -ne $DevelopmentAddress) {
        throw 'DevelopmentAddress must be one private IPv4 address'
    }
    $b = $ip.GetAddressBytes()
    if (-not ($b[0] -eq 10 -or ($b[0] -eq 192 -and $b[1] -eq 168) -or
        ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31))) { throw 'Private IPv4 required' }
    $publicKey = (Get-Content -LiteralPath $PublicKeyFile -Raw -Encoding UTF8).Trim()
    if ($publicKey -notmatch '^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})( [^\r\n]+)?$' -or
        [Convert]::FromBase64String($Matches[1]).Length -ne 51) { throw 'One Ed25519 public key required' }
    $keyHash = Get-ContentHash $PublicKeyFile
    if ((Get-ContentHash $Archive) -ine $expectedHash) { throw 'Official archive SHA256 mismatch' }
}
if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME,"$Action isolated temporary SSH on port $portNumber")) { return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Right-click the CMD file and choose Run as administrator.'
}
if ($Action -eq 'Start') { Disable-WindowQuickEdit }
$loginName = $env:USERNAME.ToLowerInvariant()
if ($loginName -notmatch '^[a-z0-9_][a-z0-9_.-]{0,63}$' -or
    (Get-LocalUser -Name $loginName).SID.Value -ne $identity.User.Value) { throw 'Local administrator account required' }
$state = $null
if (Test-Path -LiteralPath $root) {
    if ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unexpected reparse directory' }
    $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.owner -ne $ownerTag -or $state.login_name -ne $loginName -or
        $state.binary -ine (Join-Path $root 'OpenSSH-Win64/sshd.exe')) { throw 'Existing state does not match this account or directory' }
}
if ($Action -eq 'Stop') {
    if (-not $state) { throw 'No managed temporary connection found' }
    Set-Content -LiteralPath $stopFile -Value 'stop' -Encoding ASCII
    Remove-OwnRule $state
    # Also handles an abandoned launcher, checking both executable identity and start time.
    if ($state.process_id) {
        $running = Get-Process -Id $state.process_id -ErrorAction SilentlyContinue
        if ($running -and $running.Path -ieq $state.binary -and
            $running.StartTime.ToUniversalTime().Ticks.ToString() -eq $state.process_start_ticks) {
            $running.Kill()
        }
    }
    Write-Host 'Temporary access stopped. Windows services and business data were not changed.'
    return
}
if ($state -and ($state.development_address -ne $DevelopmentAddress -or $state.public_key_sha256 -ne $keyHash)) {
    throw 'Existing source or public key differs; refusing replacement'
}
if (-not $state) {
    New-Item -ItemType Directory -Path $root | Out-Null
    Protect-Path $root
    $state = [pscustomobject]@{
        owner=$ownerTag;login_name=$loginName;development_address=$DevelopmentAddress;public_key_sha256=$keyHash
        binary=(Join-Path $root 'OpenSSH-Win64/sshd.exe');rule_name=$null;process_id=$null;process_start_ticks=$null
        phase='preparing';expires_at=$null
    }
    Save-State
}
$lock = [IO.File]::Open((Join-Path $root 'access.lock'),'OpenOrCreate','ReadWrite','None')
$process = $null
$ruleCreated = $false
$sessionStarted = $false
try {
    if (Get-NetTCPConnection -LocalPort $portNumber -State Listen -ErrorAction SilentlyContinue) { throw 'Temporary port already occupied' }
    Remove-OwnRule $state
    $sessionStarted = $true
    if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
    # Install only into the private directory. Never run install-sshd.ps1 or alter in-box SSH.
    Expand-Archive -LiteralPath $Archive -DestinationPath $root -Force
    $binaryDir = Join-Path $root 'OpenSSH-Win64'
    foreach ($executable in @('sshd.exe','ssh-keygen.exe','sftp-server.exe')) {
        $signature = Get-AuthenticodeSignature (Join-Path $binaryDir $executable)
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
            throw "Microsoft signature verification failed: $executable"
        }
    }
    $hostKey = Join-Path $root 'host_ed25519'
    $keygen = Join-Path $binaryDir 'ssh-keygen.exe'
    if (-not (Test-Path -LiteralPath $hostKey)) {
        $keyProcess = Start-Process -FilePath $keygen -ArgumentList ('-q -t ed25519 -N "" -f "' + $hostKey + '"') -WindowStyle Hidden -Wait -PassThru
        if ($keyProcess.ExitCode -ne 0) { throw 'Host key generation failed' }
        $keyProcess.Dispose()
    }
    Protect-Path $hostKey
    $authorized = Join-Path $root 'authorized_keys'
    $publicKey | Set-Content -LiteralPath $authorized -Encoding ASCII
    Protect-Path $authorized
    $config = Join-Path $root 'sshd_config'
    @(
        "# $ownerTag"
        "Port $portNumber"
        'AddressFamily inet'
        'ListenAddress 0.0.0.0'
        ('HostKey "' + $hostKey.Replace('\','/') + '"')
        ('AuthorizedKeysFile "' + $authorized.Replace('\','/') + '"')
        "AllowUsers $loginName@$DevelopmentAddress"
        'AuthenticationMethods publickey'
        'PubkeyAuthentication yes'
        'PasswordAuthentication no'
        'KbdInteractiveAuthentication no'
        'PermitEmptyPasswords no'
        'DisableForwarding yes'
        'PermitTTY no'
        'LoginGraceTime 30'
        'MaxAuthTries 3'
        ('Subsystem sftp "' + (Join-Path $binaryDir 'sftp-server.exe').Replace('\','/') + '"')
    ) | Set-Content -LiteralPath $config -Encoding ASCII
    & $state.binary -t -f $config
    if ($LASTEXITCODE -ne 0) { throw 'SSH configuration validation failed' }
    $deadline = (Get-Date).AddMinutes($Minutes)
    $state.rule_name = 'LDS-Aftersales-Portable-' + [guid]::NewGuid().ToString('N')
    $state.expires_at = $deadline.ToString('o')
    $state.phase = 'starting'
    Save-State
    New-NetFirewallRule -Name $state.rule_name -DisplayName 'LDS temporary deployment - one developer PC' -Description $ownerTag `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort $portNumber -RemoteAddress $DevelopmentAddress `
        -Program $state.binary -Profile Any | Out-Null
    $ruleCreated = $true
    $reportedReady = $false
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $stopFile)) {
        $logPrefix = Join-Path $root ('connection-' + [guid]::NewGuid().ToString('N'))
        $process = Start-Process -FilePath $state.binary -ArgumentList @('-d','-e','-f',('"'+$config+'"')) `
            -WindowStyle Hidden -RedirectStandardOutput ($logPrefix+'.out.log') -RedirectStandardError ($logPrefix+'.err.log') -PassThru
        $state.process_id = $process.Id
        $state.process_start_ticks = $process.StartTime.ToUniversalTime().Ticks.ToString()
        Save-State
        $ready = $false
        for ($attempt=0; $attempt -lt 12 -and -not $process.HasExited; $attempt++) {
            if (Get-NetTCPConnection -LocalPort $portNumber -State Listen -ErrorAction SilentlyContinue | Where-Object OwningProcess -eq $process.Id) {
                $ready = $true; break
            }
            $null = $process.WaitForExit(500)
        }
        if (-not $ready) { throw "Temporary listener failed; read $logPrefix.err.log" }
        if (-not $reportedReady) {
            $state.phase = 'ready'; Save-State
            Write-Host "READY - Computer: $env:COMPUTERNAME; Account: $loginName; Port: $portNumber" -ForegroundColor Green
            Write-Host "Only $DevelopmentAddress is allowed. Expires: $deadline. Keep this window open."
            & $keygen -lf ($hostKey+'.pub')
            Write-Host 'Send the READY line and SHA256 fingerprint back. Use 2-Stop-Access.cmd to stop.'
            $reportedReady = $true
        }
        while (-not $process.WaitForExit(1000)) {
            if ((Get-Date) -ge $deadline -or (Test-Path -LiteralPath $stopFile)) { break }
        }
        if (-not $process.HasExited) { $process.Kill(); $null=$process.WaitForExit(3000) }
        $process.Dispose(); $process=$null
        # Debug server accepts one session; relaunch only the listener, never remote business commands.
        Start-Sleep -Milliseconds 300
    }
}
finally {
    try {
        if ($process) {
            if (-not $process.HasExited) { $process.Kill(); $null=$process.WaitForExit(3000) }
            $process.Dispose()
        }
        if ($ruleCreated) { Remove-OwnRule $state }
        if ($sessionStarted) {
            $state.phase='stopped'; $state.process_id=$null; $state.process_start_ticks=$null; Save-State
        }
    } finally { $lock.Dispose() }
}
