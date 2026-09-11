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
$releaseFile = Join-Path $runtimeDir 'module1-worker-release.json'

function Get-WorkerReleaseSource {
    if (-not (Test-Path -LiteralPath $releaseFile)) { return $null }
    $release = Get-Content -LiteralPath $releaseFile -Raw -Encoding utf8 | ConvertFrom-Json
    if (-not $release.source_path) { throw '后台版本配置缺少 source_path，禁止回退到开发代码' }
    $source = (Resolve-Path -LiteralPath (Join-Path $projectRoot $release.source_path)).Path
    $allowed = [System.IO.Path]::GetFullPath((Join-Path $runtimeDir 'releases')) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $source.StartsWith($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw '后台版本目录必须位于 .runtime/releases 内'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $source 'aftersales_workbench/workflows/module1_worker_cli.py'))) {
        throw '后台版本缺少入口文件，禁止回退到开发代码'
    }
    return $source
}

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
            Write-Output "售后后台运行器（模块1+模块2+模块3）已启动，PID=$($existing.Id)"
            exit 0
        }
        if (-not (Test-Path -LiteralPath $workerExe)) {
            throw "缺少运行入口，请先执行：.\.venv\Scripts\python.exe -m pip install -e `".[dev]`""
        }
        $releaseSource = Get-WorkerReleaseSource
        Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
        $arguments = @('--forever', '--stop-file', '.runtime/module1-worker.stop')
        $previousPythonPath = $env:PYTHONPATH
        $previousRuntimeRoot = $env:AFTERSALES_RUNTIME_ROOT
        try {
            $env:AFTERSALES_RUNTIME_ROOT = $projectRoot
            if ($releaseSource) {
                $env:PYTHONPATH = $releaseSource
                $resolvedPackage = & (Join-Path $projectRoot '.venv/Scripts/python.exe') -c "import os, pathlib, aftersales_workbench; root=pathlib.Path(os.environ['PYTHONPATH']).resolve(); actual=pathlib.Path(aftersales_workbench.__file__).resolve(); assert actual.is_relative_to(root); print('release_import_ok')"
                if ($LASTEXITCODE -ne 0 -or $resolvedPackage -ne 'release_import_ok') {
                    throw '后台版本导入验证失败，未启动'
                }
            }
            $process = Start-Process `
                -FilePath $workerExe `
                -ArgumentList $arguments `
                -WorkingDirectory $projectRoot `
                -WindowStyle Hidden `
                -RedirectStandardOutput $stdoutLog `
                -RedirectStandardError $stderrLog `
                -PassThru
        }
        finally {
            $env:PYTHONPATH = $previousPythonPath
            $env:AFTERSALES_RUNTIME_ROOT = $previousRuntimeRoot
        }
        Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
        Start-Sleep -Seconds 1
        if ($process.HasExited) {
            throw "售后后台运行器启动失败，请查看 $stderrLog"
        }
        Write-Output "售后后台运行器（模块1+模块2+模块3）已启动，PID=$($process.Id)"
        if ($releaseSource) { Write-Output "后台代码目录：$releaseSource" }
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
            Write-Output '售后后台运行器（模块1+模块2+模块3）：未运行'
            exit 1
        }
        Write-Output "售后后台运行器（模块1+模块2+模块3）：运行中，PID=$($existing.Id)"
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
