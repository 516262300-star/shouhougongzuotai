#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$Elevated)

$ErrorActionPreference = 'Stop'
if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, 'Collect elevated SSH file access diagnostics')) { return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    if ($Elevated) { throw 'Windows 未授予管理员令牌，停止诊断。' }
    try {
        $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-Elevated') `
            -Verb RunAs -WindowStyle Normal -Wait -PassThru
        if ($child.ExitCode -ne 0) { throw "管理员诊断退出码：$($child.ExitCode)" }
    }
    catch { Write-Error ('无法打开管理员诊断窗口：' + $_.Exception.Message) }
    return
}

function Invoke-SshProbeProcess([string]$Binary, [string[]]$NativeArguments, [int]$TimeoutMilliseconds = 8000) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Binary
    $start.Arguments = (($NativeArguments | ForEach-Object {
        if ($_ -match '["\r\n]') { throw '诊断参数含不支持的引号或换行。' }
        '"' + $_ + '"'
    }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $started = $false
    try {
        $started = $process.Start()
        if (-not $started) { throw '诊断子进程未启动。' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $timedOut = -not $process.WaitForExit($TimeoutMilliseconds)
        if ($timedOut) {
            # 仅终止本函数刚创建的调试进程，不查找或停止任何现有服务/业务进程。
            $process.Kill()
            if (-not $process.WaitForExit(3000)) { throw '诊断进程未及时退出。' }
        }
        if (-not $stdout.Wait(3000) -or -not $stderr.Wait(3000)) { throw '读取诊断输出超时。' }
        [pscustomobject]@{ ExitCode = $process.ExitCode; TimedOut = $timedOut; Stdout = $stdout.Result; Stderr = $stderr.Result }
    }
    finally {
        if ($started -and -not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(3000) }
        $process.Dispose()
    }
}

$report = [Collections.Generic.List[string]]::new()
$report.Add('LDS SSH elevated access probe - ' + (Get-Date -Format o))
$report.Add('Identity: ' + $identity.Name + '; SID: ' + $identity.User.Value + '; ElevatedAdmin: ' + $isAdmin)
$report.Add('64bitProcess: ' + [Environment]::Is64BitProcess)
$configPath = Join-Path $env:ProgramData 'ssh/sshd_config'
$binaryPath = Join-Path $env:WINDIR 'System32/OpenSSH/sshd.exe'
$canRead = $false
$managedConfig = $false
$reportPath = Join-Path $PSScriptRoot ('ssh-access-probe-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')
try {
    $report.Add("`r`n[PowerShell config read]")
    try {
        $configBytes = [IO.File]::ReadAllBytes($configPath)
        $canRead = $true
        $managedConfig = [Text.Encoding]::ASCII.GetString($configBytes).StartsWith('# LDS-Office-Deployment-SSH-v1')
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $digest = [BitConverter]::ToString($sha.ComputeHash($configBytes)).Replace('-', '') }
        finally { $sha.Dispose() }
        $report.Add("ReadSucceeded: True; Bytes: $($configBytes.Length); SHA256: $digest; ManagedConfig: $managedConfig")
    }
    catch { $report.Add('ReadSucceeded: False; ' + $_.Exception.ToString()) }
    $report.Add("`r`n[File metadata and ACL]")
    foreach ($path in @($env:ProgramData, (Join-Path $env:ProgramData 'ssh'), $configPath)) {
        try {
            $item = Get-Item -LiteralPath $path -Force
            $acl = Get-Acl -LiteralPath $path
            $report.Add(([pscustomobject]@{Path=$path; Attributes=[string]$item.Attributes; Owner=$acl.Owner; SDDL=$acl.Sddl} | Format-List | Out-String -Width 240))
        }
        catch { $report.Add('Metadata read failed: ' + $_.Exception.Message) }
    }
    $report.Add("`r`n[Registered security software]")
    try { $report.Add((Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | Select-Object displayName,productState | Format-List | Out-String -Width 240)) }
    catch { $report.Add($_.Exception.Message) }
    $report.Add("`r`n[Service identity and status]")
    $service = Get-CimInstance Win32_Service -Filter "Name='sshd'"
    $report.Add(($service | Select-Object Name,State,StartMode,StartName,PathName,ExitCode | Format-List | Out-String -Width 240))
    $report.Add("`r`n[Service token restrictions]")
    $report.Add((& sc.exe qsidtype sshd 2>&1 | Out-String))
    $report.Add((& sc.exe qprivs sshd 2>&1 | Out-String))
    $report.Add("`r`n[sshd configuration validation - does not listen]")
    $result = Invoke-SshProbeProcess -Binary $binaryPath -NativeArguments @('-ddd', '-e', '-t', '-f', $configPath)
    $report.Add(($result | Format-List | Out-String -Width 240))
    $report.Add("`r`n[Bounded foreground startup]")
    if ($canRead -and $managedConfig -and $result.ExitCode -eq 0 -and -not $result.TimedOut -and $service.State -eq 'Stopped') {
        # 使用原来的密钥认证/来源账户限制，不改防火墙。最多运行8秒，随后关闭本诊断进程。
        $result = Invoke-SshProbeProcess -Binary $binaryPath -NativeArguments @('-ddd', '-e', '-f', $configPath)
        $report.Add(($result | Format-List | Out-String -Width 240))
    }
    else { $report.Add('Skipped: configuration unreadable, unmanaged, invalid, or service not stopped.') }
}
catch { $report.Add('Probe error: ' + $_.Exception.ToString()) }
finally {
    [IO.File]::WriteAllLines($reportPath, $report, [Text.UTF8Encoding]::new($true))
    Write-Host "诊断完成，请发送文件：$reportPath"
    Write-Host '未修改权限、服务配置、防火墙或售后数据。'
    if ($Elevated) { Read-Host '按回车关闭这个窗口' | Out-Null }
}
