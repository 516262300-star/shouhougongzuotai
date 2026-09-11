param([string]$SourceFile, [string]$TestRoot, [string]$Case)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SourceFile, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($function in $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)) { Invoke-Expression $function.Extent.Text }
$projectRoot = $TestRoot
$runtimeDir = Join-Path $TestRoot '.runtime'
$source = Join-Path $runtimeDir 'releases/candidate/src'
$pointer = Join-Path $runtimeDir 'workbench-web-release.json'
foreach ($name in @('aftersales_workbench/main.py', 'aftersales_workbench/core/runtime_paths.py', '../frontend/dist/client/index.html')) {
    $path = Join-Path $source $name
    New-Item -ItemType Directory -Path (Split-Path $path) -Force | Out-Null
    'fixture' | Set-Content -LiteralPath $path
}
if ($Case -eq 'legacy') {
    if ($null -ne (Get-WorkbenchWebReleaseSource)) { throw 'Unexpected release without pointer' }
} else {
    @{ source_path = '.runtime/releases/candidate/src' } | ConvertTo-Json | Set-Content -LiteralPath $pointer
    switch ($Case) {
        'corrupt' { '{}' | Set-Content -LiteralPath $pointer }
        'outside' { @{ source_path = '.' } | ConvertTo-Json | Set-Content -LiteralPath $pointer }
        'missing_source' { Remove-Item -LiteralPath (Join-Path $source 'aftersales_workbench/main.py') }
        'missing_index' { Remove-Item -LiteralPath (Join-Path $source '../frontend/dist/client/index.html') }
    }
    $failed = $false
    try { $actual = Get-WorkbenchWebReleaseSource } catch { $failed = $true }
    if ($Case -eq 'valid') {
        if ($failed -or $actual -ne $source) { throw 'Valid release not selected' }
    } elseif (-not $failed) { throw 'Invalid release must not fall back' }
}
Write-Output "PASS $Case"
