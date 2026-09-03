[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action = 'Status'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeDir = Join-Path $projectRoot '.runtime'
$pidFile = Join-Path $runtimeDir 'module1-worker.pid'
$stopFile = Join-Path $runtimeDir 'module1-worker.stop'
$stdoutLog = Join-Path $runtimeDir 'module1-worker.log'
$stderrLog = Join-Path $runtimeDir 'module1-worker-error.log'
$workerExe = Join-Path $projectRoot '.venv\Scripts\aftersales-run-module1.exe'

function Get-Module1WorkerProcess {
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
    try {
        $expectedPath = [System.IO.Path]::GetFullPath($workerExe)
        $actualPath = [System.IO.Path]::GetFullPath($process.Path)
        if (-not [string]::Equals($expectedPath, $actualPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
    }
    catch {
        return $null
    }
    return $process
}

switch ($Action) {
    'Start' {
        New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
        $existing = Get-Module1WorkerProcess
        if ($null -ne $existing) {
            Write-Output "售后后台运行器（模块1+模块3）已启动，PID=$($existing.Id)"
            exit 0
        }
        if (-not (Test-Path -LiteralPath $workerExe)) {
            throw "缺少运行入口，请先执行：.\.venv\Scripts\python.exe -m pip install -e `".[dev]`""
        }
        Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
        $arguments = @('--forever', '--stop-file', '.runtime/module1-worker.stop')
        $process = Start-Process `
            -FilePath $workerExe `
            -ArgumentList $arguments `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru
        Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
        Start-Sleep -Seconds 1
        if ($process.HasExited) {
            throw "售后后台运行器启动失败，请查看 $stderrLog"
        }
        Write-Output "售后后台运行器（模块1+模块3）已启动，PID=$($process.Id)"
        Write-Output "运行日志：$stdoutLog"
        Write-Output "错误日志：$stderrLog"
    }
    'Stop' {
        $existing = Get-Module1WorkerProcess
        if ($null -eq $existing) {
            Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
            Write-Output '售后后台运行器当前未运行'
            exit 0
        }
        New-Item -ItemType File -Path $stopFile -Force | Out-Null
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            Start-Sleep -Seconds 1
            $existing.Refresh()
            if ($existing.HasExited) {
                Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
                Write-Output '售后后台运行器已安全停止'
                exit 0
            }
        }
        Write-Output '已发出安全停止请求；当前同步周期尚未结束，请稍后再次查看状态'
    }
    'Status' {
        $existing = Get-Module1WorkerProcess
        if ($null -eq $existing) {
            Write-Output '售后后台运行器（模块1+模块3）：未运行'
            exit 1
        }
        Write-Output "售后后台运行器（模块1+模块3）：运行中，PID=$($existing.Id)"
        Write-Output "运行日志：$stdoutLog"
        Write-Output "错误日志：$stderrLog"
        if (Test-Path -LiteralPath $stdoutLog) {
            $lastCycle = Get-Content -LiteralPath $stdoutLog -Tail 1
            if ($lastCycle) {
                Write-Output "最近周期：$lastCycle"
            }
        }
    }
}
