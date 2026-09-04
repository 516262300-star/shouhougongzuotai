[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$LegacyRoot,
    [string]$EnvPath,
    [switch]$EnableModules
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($EnvPath)) {
    $EnvPath = Join-Path $repoRoot '.env'
}
$resolvedEnvPath = [IO.Path]::GetFullPath($EnvPath)
if ([IO.Path]::GetFileName($resolvedEnvPath) -ne '.env' -or
    [IO.Path]::GetDirectoryName($resolvedEnvPath) -ne [IO.Path]::GetFullPath($repoRoot)) {
    throw '为避免覆盖其他配置，本工具只允许更新当前仓库根目录的 .env。'
}
if (-not (Test-Path -LiteralPath $resolvedEnvPath -PathType Leaf)) {
    throw "未找到本机配置：$resolvedEnvPath"
}

$legacyPath = Join-Path $LegacyRoot 'application\models\Tm_api_model.php'
if (-not (Test-Path -LiteralPath $legacyPath -PathType Leaf)) {
    throw "未找到旧天猫接口文件：$legacyPath"
}

function Get-RefundSessionKey {
    param(
        [string]$Text,
        [string]$StoreCode,
        [int]$ShopNumber
    )
    $escaped = [regex]::Escape($StoreCode)
    $block = [regex]::Match(
        $Text,
        ('(?s)(?:if|elseif)\s*\(\s*\$store\s*==\s*[''"]' + $escaped + '[''"]\s*\)\s*\{(?<body>.*?)\}'),
        [Text.RegularExpressions.RegexOptions]::Singleline
    )
    if (-not $block.Success) {
        throw "旧源码中没有找到天猫 ${ShopNumber} 店退款子账号配置"
    }
    $session = [regex]::Match(
        $block.Groups['body'].Value,
        '\$this->sessionkey\s*=\s*["'']([^"'']+)["'']'
    )
    if (-not $session.Success -or [string]::IsNullOrWhiteSpace($session.Groups[1].Value)) {
        throw "旧源码中天猫 ${ShopNumber} 店退款子账号 SessionKey 为空"
    }
    return $session.Groups[1].Value.Trim()
}

function Set-DotEnvValue {
    param(
        [string]$Content,
        [string]$Name,
        [string]$Value
    )
    $pattern = '(?m)^' + [regex]::Escape($Name) + '=.*$'
    $line = "$Name=$Value"
    if ([regex]::IsMatch($Content, $pattern)) {
        return [regex]::Replace(
            $Content,
            $pattern,
            [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $line }
        )
    }
    if ($Content.Length -gt 0 -and -not $Content.EndsWith("`n")) {
        $Content += "`r`n"
    }
    return $Content + $line + "`r`n"
}

$legacy = Get-Content -LiteralPath $legacyPath -Raw
$storeCodes = @('refund', 'refundp2-', 'refundp3-', 'refundp4-', 'refundp5-')
$updates = [ordered]@{}
for ($index = 0; $index -lt $storeCodes.Count; $index++) {
    $shopNumber = $index + 1
    $updates["TMALL_SHOP_${shopNumber}_REFUND_SESSION_KEY"] = Get-RefundSessionKey `
        -Text $legacy `
        -StoreCode $storeCodes[$index] `
        -ShopNumber $shopNumber
}
$updates['TMALL_REFUND_ENABLED_SHOP_NUMBERS'] = '[1,2,3,4,5]'

if ($EnableModules) {
    $updates['TMALL_SYNC_ENABLED'] = 'true'
    $updates['TMALL_MODULE123_TRIAL_ENABLED'] = 'true'
    $updates['TMALL_WRITE_ENABLED'] = 'true'
    $updates['MODULE1_TMALL_REFUND_EXECUTION_ENABLED'] = 'true'
    $updates['MODULE2_TMALL_REFUND_EXECUTION_ENABLED'] = 'true'
}

$runtime = Join-Path $repoRoot '.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$backupPath = Join-Path $runtime (
    "env-before-tmall-refund-credentials-{0}.bak" -f (Get-Date -Format 'yyyyMMdd-HHmmss')
)
Copy-Item -LiteralPath $resolvedEnvPath -Destination $backupPath

$content = [IO.File]::ReadAllText($resolvedEnvPath)
foreach ($entry in $updates.GetEnumerator()) {
    $content = Set-DotEnvValue $content $entry.Key ([string]$entry.Value)
}
[IO.File]::WriteAllText($resolvedEnvPath, $content, [Text.UTF8Encoding]::new($false))

Write-Output '已迁移天猫前五店退款子账号凭证；适家未配置、未加入自动退款白名单。'
Write-Output "修改前备份：$backupPath"
if ($EnableModules) {
    Write-Output '已开启天猫同步、模块1/2退款写入；模块3沿用平台退款成功后的 ERP 闭环。'
} else {
    Write-Output '模块写入开关未自动开启。'
}
