param([string]$SourceFile, [string]$TestRoot)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SourceFile, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Invalid script syntax' }
$function = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Save-WorkerLogsBeforeStart'
}, $false)
Invoke-Expression $function[0].Extent.Text
$runtimeDir = Join-Path $TestRoot '.runtime'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$stdoutLog = Join-Path $runtimeDir 'module1-worker.log'
$stderrLog = Join-Path $runtimeDir 'module1-worker-error.log'
$releaseFile = Join-Path $runtimeDir 'module1-worker-release.json'
Set-Content -LiteralPath $stdoutLog -Value 'first-cycle' -Encoding utf8
Set-Content -LiteralPath $stderrLog -Value 'first-error' -Encoding utf8
Set-Content -LiteralPath $releaseFile -Value '{"code_commit":"test"}' -Encoding utf8
Save-WorkerLogsBeforeStart
if ((Get-Content -LiteralPath $stdoutLog -Raw).Trim() -ne 'first-cycle') { throw 'Source changed' }
Set-Content -LiteralPath $stdoutLog -Value 'second-cycle' -Encoding utf8
Save-WorkerLogsBeforeStart
$files = @(Get-ChildItem -LiteralPath (Join-Path $runtimeDir 'worker-log-history') -Recurse -Filter 'module1-worker.log')
if ($files.Count -ne 2) { throw 'Restart overwrote history' }
$content = @($files | ForEach-Object { (Get-Content -LiteralPath $_.FullName -Raw).Trim() })
if ('first-cycle' -notin $content -or 'second-cycle' -notin $content) { throw 'Missing cycle evidence' }
Write-Output 'PASS log-preservation'
