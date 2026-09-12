#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$Elevated, [switch]$Stop)

$ErrorActionPreference = 'Stop'
if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, 'Manage temporary deployment connection')) { return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($Elevated) { throw '管理员权限未授予。' }
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-Elevated')
    if ($Stop) { $arguments += '-Stop' }
    $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -Verb RunAs -WindowStyle Normal -Wait -PassThru
    if ($child.ExitCode -ne 0) { throw "临时连接退出码：$($child.ExitCode)" }
    return
}

$stateRoot = Join-Path $env:ProgramData 'LdsAftersalesDeployment'
$state = Get-Content -LiteralPath (Join-Path $stateRoot 'access-state.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($state.owner -ne 'LDS-Office-Deployment-SSH-v1' -or $state.login_name -ne $env:USERNAME.ToLowerInvariant()) {
    throw '当前账户或部署状态不匹配，未开启连接。'
}
$sourceIp = $null
if (-not [Net.IPAddress]::TryParse($state.development_address, [ref]$sourceIp) -or
    $sourceIp.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
    $sourceIp.ToString() -ne $state.development_address) { throw '受管来源不是单个 IPv4 地址。' }
$sourceBytes = $sourceIp.GetAddressBytes()
if (-not ($sourceBytes[0] -eq 10 -or ($sourceBytes[0] -eq 192 -and $sourceBytes[1] -eq 168) -or
    ($sourceBytes[0] -eq 172 -and $sourceBytes[1] -ge 16 -and $sourceBytes[1] -le 31))) { throw '受管来源不是私有局域网地址。' }
$stopPath = Join-Path $stateRoot 'temporary-access.stop'
if ($Stop) { Set-Content -LiteralPath $stopPath -Value 'stop' -Encoding ASCII; Write-Host '已请求结束临时连接。'; return }
$configPath = Join-Path $env:ProgramData 'ssh/sshd_config'
$binaryPath = Join-Path $env:WINDIR 'System32/OpenSSH/sshd.exe'
$configLines = @(Get-Content -LiteralPath $configPath -Encoding ASCII | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith('#') })
$expected = [ordered]@{
    Port='22'; AddressFamily='inet'; ListenAddress='0.0.0.0'
    AllowUsers=($state.login_name + '@' + $state.development_address)
    AuthenticationMethods='publickey'; PubkeyAuthentication='yes'; PasswordAuthentication='no'
    PermitEmptyPasswords='no'; AuthorizedKeysFile='__PROGRAMDATA__/ssh/lds_aftersales_authorized_keys'
    AllowAgentForwarding='no'; AllowTcpForwarding='no'; PermitTTY='no'; Subsystem='sftp sftp-server.exe'
}
if ($configLines.Count -ne $expected.Count) { throw 'SSH 配置已变化，停止临时接管。' }
foreach ($name in $expected.Keys) {
    $matching = @($configLines | Where-Object { $_ -match ('^\s*' + [regex]::Escape($name) + '\s+') })
    if ($matching.Count -ne 1 -or ($matching[0].Trim() -replace '\s+', ' ') -cne ($name + ' ' + $expected[$name])) {
        throw "SSH 配置保护校验失败：$name"
    }
}
$ruleName = 'LDS-Aftersales-Deployment-SSH'
$rule = Get-NetFirewallRule -Name $ruleName
$address = $rule | Get-NetFirewallAddressFilter
$port = $rule | Get-NetFirewallPortFilter
$application = $rule | Get-NetFirewallApplicationFilter
if ($rule.Direction -ne 'Inbound' -or $rule.Action -ne 'Allow' -or
    @($address.RemoteAddress).Count -ne 1 -or $address.RemoteAddress -ne $state.development_address -or
    @($port.LocalPort).Count -ne 1 -or $port.LocalPort -ne '22' -or [string]$port.Protocol -notin @('TCP','6') -or
    [IO.Path]::GetFullPath($application.Program) -ine [IO.Path]::GetFullPath($binaryPath)) {
    throw '已有防火墙规则与限定来源/程序/端口不匹配，未启用。'
}
if ((Get-Service sshd).Status -ne 'Stopped' -or (Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue)) {
    throw '已有 SSH 服务或监听器，不启动第二个。'
}
& $binaryPath -t -f $configPath
if ($LASTEXITCODE -ne 0) { throw '配置验证失败。' }
$lock = [IO.File]::Open((Join-Path $stateRoot 'temporary-access.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
$process = $null
$changedRule = $false
$wasEnabled = [string]$rule.Enabled -eq 'True'
try {
    if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
    Enable-NetFirewallRule -Name $ruleName | Out-Null
    $changedRule = $true
    $deadline = (Get-Date).AddMinutes(60)
    Write-Host "仅允许开发机 $($state.development_address) 使用既有密钥连接；最长60分钟。"
    Write-Host '请保持本窗口打开。结束时运行 2-Stop-Temporary.cmd。'
    $reportedReady = $false
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $stopPath)) {
        if ((Get-Service sshd).Status -eq 'Running') { break }
        $logPrefix = Join-Path $stateRoot ('temporary-console-' + [guid]::NewGuid().ToString('N'))
        $process = Start-Process -FilePath $binaryPath -ArgumentList @('-d', '-e', '-f', ('"' + $configPath + '"')) `
            -WindowStyle Hidden -RedirectStandardOutput ($logPrefix + '.out.log') -RedirectStandardError ($logPrefix + '.err.log') -PassThru
        $ready = $false
        for ($attempt = 0; $attempt -lt 8 -and -not $process.HasExited; $attempt++) {
            $listener = Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue | Where-Object OwningProcess -eq $process.Id
            if ($listener) { $ready = $true; break }
            $null = $process.WaitForExit(500)
        }
        if (-not $ready) {
            throw "临时进程未监听，请发送诊断日志：$logPrefix.err.log"
        }
        if (-not $reportedReady) {
            Write-Host '临时连接已就绪，请回复“已就绪”，并保持窗口打开。' -ForegroundColor Green
            & (Join-Path $env:WINDIR 'System32/OpenSSH/ssh-keygen.exe') -lf (Join-Path $env:ProgramData 'ssh/ssh_host_ed25519_key.pub')
            $reportedReady = $true
        }
        while (-not $process.WaitForExit(1000)) {
            if ((Get-Date) -ge $deadline -or (Test-Path -LiteralPath $stopPath)) { break }
        }
        if (-not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(3000) }
        $process.Dispose()
        $process = $null
        # 调试方式每次接一个连接，结束后重新等待；不重试远程命令或售后动作。
        Start-Sleep -Milliseconds 300
    }
}
finally {
    if ($process) {
        if (-not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(3000) }
        $process.Dispose()
    }
    if ($changedRule -and -not $wasEnabled -and (Get-Service sshd).Status -ne 'Running') {
        Disable-NetFirewallRule -Name $ruleName | Out-Null
    }
    $lock.Dispose()
    Write-Host '临时连接已结束。正式 SSH 服务的修复与验收另行完成。'
    if ($Elevated) { Read-Host '按回车关闭窗口' | Out-Null }
}
