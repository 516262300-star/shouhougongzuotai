param(
    [Parameter(Mandatory=$true)][string]$SourceRoot,
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$OutputDirectory = '.runtime/erp-client-build'
)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $SourceRoot).Path
$entry = Join-Path $source 'tools/leedis_desktop/desktop_app.py'
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    throw 'SourceRoot must contain tools/leedis_desktop/desktop_app.py'
}
$output = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $output -Force | Out-Null
$dist = Join-Path $output 'dist'
$build = Join-Path $output 'build'
# Permanent _internal resources survive cleanup of the Windows Temp directory.
& $Python -m PyInstaller --noconfirm --onedir --windowed --name LeedisClient-Workbench --hidden-import keyring.backends.Windows --collect-all certifi --distpath $dist --workpath $build --specpath $output $entry
if ($LASTEXITCODE -ne 0) { throw 'ERP client build failed' }
$package = Join-Path $dist 'LeedisClient-Workbench'
$exe = Join-Path $package 'LeedisClient-Workbench.exe'
$certificate = Join-Path $package '_internal/certifi/cacert.pem'
if (-not (Test-Path -LiteralPath $certificate -PathType Leaf)) { throw 'Bundled TLS certificates missing' }
$report = Join-Path $output 'self-test.json'
$process = Start-Process -FilePath $exe -ArgumentList @('--self-test', ('"' + $report + '"')) -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $report)) { throw 'ERP client self-test failed' }
$check = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
if ($check.ok -ne $true) { throw 'ERP client self-test did not pass' }
$manifest = @(Get-ChildItem -LiteralPath $package -Recurse -File | ForEach-Object {
    @{ path=$_.FullName.Substring($package.Length + 1).Replace('\','/'); sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
})
$manifest | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $output 'package-sha256.json') -Encoding UTF8
Write-Output "Verified package: $package"
Write-Output 'Deploy the executable and the entire _internal directory together. This script does not deploy or restart services.'
