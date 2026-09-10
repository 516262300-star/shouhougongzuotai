param([string]$SourceFile, [string]$TestRoot, [string]$Case)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SourceFile, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($function in $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)) { Invoke-Expression $function.Extent.Text }
$projectRoot = $TestRoot
$runtimeDir = Join-Path $TestRoot '.runtime'
$releaseFile = Join-Path $runtimeDir 'module1-worker-release.json'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
if ($Case -eq 'absent') {
    if ($null -ne (Get-WorkerReleaseSource)) { throw 'Must preserve default when not pinned' }
} else {
    $relative = if ($Case -eq 'outside') { 'outside/src' } else { '.runtime/releases/example/src' }
    $source = Join-Path $TestRoot $relative
    $entry = Join-Path $source 'aftersales_workbench/workflows/module1_worker_cli.py'
    New-Item -ItemType Directory -Path (Split-Path $entry) -Force | Out-Null
    if ($Case -ne 'missing_entry') { New-Item -ItemType File -Path $entry | Out-Null }
    @{source_path=$relative} | ConvertTo-Json | Set-Content -LiteralPath $releaseFile -Encoding utf8
    if ($Case -eq 'corrupt') { '{' | Set-Content -LiteralPath $releaseFile }
    $rejected = $false
    try { $resolved = Get-WorkerReleaseSource } catch { $rejected = $true }
    if ($Case -eq 'valid') {
        if ($rejected -or $resolved -ne $source) { throw 'Valid pin was not loaded' }
    } elseif (-not $rejected) { throw 'Invalid pin must not fall back to development code' }
}
Write-Output "PASS $Case"
