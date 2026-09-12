#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidateSet('Inspect', 'Install', 'Disable')]
    [string]$Action = 'Inspect',
    [string]$DevelopmentAddress,
    [string]$PublicKeyFile
)

$ErrorActionPreference = 'Stop'
$ownerTag = 'LDS-Office-Deployment-SSH-v1'
$stateRoot = Join-Path $env:ProgramData 'LdsAftersalesDeployment'
$statePath = Join-Path $stateRoot 'access-state.json'
$sshRoot = Join-Path $env:ProgramData 'ssh'
$configPath = Join-Path $sshRoot 'sshd_config'
$keyPath = Join-Path $sshRoot 'lds_aftersales_authorized_keys'
$ruleName = 'LDS-Aftersales-Deployment-SSH'
$defaultRuleName = 'OpenSSH-Server-In-TCP'
$sshBin = Join-Path $env:WINDIR 'System32/OpenSSH'

function Protect-AdminPath([string]$Path) {
    $systemSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $adminSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer) {
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    }
    else {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
        $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($adminSid)
    foreach ($sid in @($systemSid, $adminSid)) {
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', $inheritance, 'None', 'Allow'
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Read-ManagedState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    $result = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($result.owner -ne $ownerTag) { throw '已有未知部署配置，停止操作。' }
    return $result
}

function Save-ManagedState($Value) {
    $Value | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    Protect-AdminPath $statePath
}

if ($Action -eq 'Inspect') {
    $service = Get-Service -Name sshd -ErrorAction SilentlyContinue
    [pscustomobject]@{
        computer = $env:COMPUTERNAME
        user = $env:USERNAME
        os = (Get-CimInstance Win32_OperatingSystem).Caption
        ssh_service = $(if ($service) { [string]$service.Status } else { 'NotInstalled' })
        managed_state_exists = (Test-Path -LiteralPath $statePath)
    } | ConvertTo-Json
    return
}

if ($Action -eq 'Install') {
    $parsedAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($DevelopmentAddress, [ref]$parsedAddress) -or
        $parsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $parsedAddress.ToString() -ne $DevelopmentAddress) {
        throw 'DevelopmentAddress 必须是开发机的完整 IPv4 地址。'
    }
    $octets = $parsedAddress.GetAddressBytes()
    if (-not ($octets[0] -eq 10 -or
        ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) -or
        ($octets[0] -eq 192 -and $octets[1] -eq 168))) {
        throw '只允许局域网私有 IPv4 地址，不接受公网地址或整个网段。'
    }
    $publicKey = (Get-Content -LiteralPath $PublicKeyFile -Raw -Encoding UTF8).Trim()
    if ($publicKey -notmatch '^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})( [^\r\n]+)?$' -or
        [Convert]::FromBase64String($Matches[1]).Length -ne 51) {
        throw '需要一条有效的 Ed25519 公钥，不能使用私钥或多行文件。'
    }
    $keyHash = (Get-FileHash -LiteralPath $PublicKeyFile -Algorithm SHA256).Hash
    $loginName = $env:USERNAME.ToLowerInvariant()
    if ($loginName -notmatch '^[a-z0-9_][a-z0-9_.-]{0,63}$') {
        throw '当前账户名需要单独适配，尚未安装。'
    }
}

if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, "$Action deployment SSH access")) { return }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '请右键启动文件，选择“以管理员身份运行”。'
}
$state = Read-ManagedState

if ($Action -eq 'Disable') {
    if (-not $state) { throw '未找到本工具管理的连接，不改动其他 SSH 服务。' }
    Stop-Service -Name sshd -ErrorAction SilentlyContinue
    Set-Service -Name sshd -StartupType Disabled -ErrorAction SilentlyContinue
    Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Disable-NetFirewallRule | Out-Null
    Get-NetFirewallRule -Name $defaultRuleName -ErrorAction SilentlyContinue | Disable-NetFirewallRule | Out-Null
    $state.phase = 'disabled'
    Save-ManagedState $state
    Write-Output '部署连接已关闭。数据库和企微后台未改动；配置保留供恢复。'
    return
}

$localAccount = Get-LocalUser -Name $loginName -ErrorAction Stop
if ($localAccount.SID.Value -ne $identity.User.Value) {
    throw '请使用正式运行机上的本地管理员账户运行，当前身份不匹配。'
}
if ($state) {
    if ($state.development_address -ne $DevelopmentAddress -or
        $state.public_key_sha256 -ne $keyHash -or $state.login_name -ne $loginName) {
        throw '已有部署连接与本次地址、账户或公钥不同，禁止覆盖。'
    }
}
else {
    if ((Get-Service sshd -ErrorAction SilentlyContinue) -or
        (Test-Path -LiteralPath $configPath) -or (Test-Path -LiteralPath $keyPath) -or
        (Test-Path -LiteralPath $stateRoot) -or
        (Get-NetFirewallRule -Name $ruleName,$defaultRuleName -ErrorAction SilentlyContinue) -or
        (Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue)) {
        throw '检测到已有 SSH、部署目录或 22 端口占用。停止安装，保留原配置。'
    }
    New-Item -ItemType Directory -Path $stateRoot | Out-Null
    Protect-AdminPath $stateRoot
    $state = [pscustomobject]@{
        owner = $ownerTag
        development_address = $DevelopmentAddress
        public_key_sha256 = $keyHash
        login_name = $loginName
        phase = 'preparing'
    }
    Save-ManagedState $state
}

try {
    Write-Output '正在安装 Windows OpenSSH Server；此步骤可能需要数分钟，请保持联网。'
    $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
    if ($capability.State -ne 'Installed') {
        $installation = Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
        if ($installation.RestartNeeded) { throw 'Windows 要求重启。重启并登录后重新运行本文件。' }
    }
    Stop-Service -Name sshd -ErrorAction SilentlyContinue
    Set-Service -Name sshd -StartupType Manual
    Get-NetFirewallRule -Name $defaultRuleName -ErrorAction SilentlyContinue | Disable-NetFirewallRule | Out-Null
    Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Disable-NetFirewallRule | Out-Null
    New-Item -ItemType Directory -Path $sshRoot -Force | Out-Null
    if (Test-Path -LiteralPath $configPath) {
        $backupPath = Join-Path $stateRoot ('sshd-config-before-' + [guid]::NewGuid().ToString('N') + '.txt')
        Copy-Item -LiteralPath $configPath -Destination $backupPath
    }
    $publicKey | Set-Content -LiteralPath $keyPath -Encoding ASCII
    Protect-AdminPath $keyPath
    $config = @(
        "# $ownerTag"
        'Port 22'
        'AddressFamily inet'
        'ListenAddress 0.0.0.0'
        "AllowUsers $loginName@$DevelopmentAddress"
        'AuthenticationMethods publickey'
        'PubkeyAuthentication yes'
        'PasswordAuthentication no'
        'PermitEmptyPasswords no'
        'AuthorizedKeysFile __PROGRAMDATA__/ssh/lds_aftersales_authorized_keys'
        'AllowAgentForwarding no'
        'AllowTcpForwarding no'
        'PermitTTY no'
        'Subsystem sftp sftp-server.exe'
    )
    $config | Set-Content -LiteralPath $configPath -Encoding ASCII
    Protect-AdminPath $configPath
    & (Join-Path $sshBin 'ssh-keygen.exe') -A
    if ($LASTEXITCODE -ne 0) { throw '生成主机密钥失败。' }
    & (Join-Path $sshBin 'sshd.exe') -t -f $configPath
    if ($LASTEXITCODE -ne 0) { throw 'SSH 配置检查失败，未开放连接。' }
    Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -Name $ruleName -DisplayName 'LDS workbench deployment from development PC only' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 `
        -RemoteAddress $DevelopmentAddress -Program (Join-Path $sshBin 'sshd.exe') `
        -Profile Any | Out-Null
    Start-Service -Name sshd
    (Get-Service sshd).WaitForStatus('Running', [TimeSpan]::FromSeconds(15))
    Set-Service -Name sshd -StartupType Automatic
    $state.phase = 'ready'
    Save-ManagedState $state
    Write-Output "部署连接已准备好。电脑：$env:COMPUTERNAME；账户：$loginName；仅允许：$DevelopmentAddress"
    Write-Output '请把本窗口最后的成功提示和下面的主机指纹发给部署人员：'
    & (Join-Path $sshBin 'ssh-keygen.exe') -lf (Join-Path $sshRoot 'ssh_host_ed25519_key.pub')
    Write-Output '这只完成部署连接，尚未安装或接管售后业务。'
}
catch {
    Stop-Service -Name sshd -ErrorAction SilentlyContinue
    Set-Service -Name sshd -StartupType Disabled -ErrorAction SilentlyContinue
    Get-NetFirewallRule -Name $ruleName,$defaultRuleName -ErrorAction SilentlyContinue | Disable-NetFirewallRule | Out-Null
    $state.phase = 'failed'
    Save-ManagedState $state
    throw
}
