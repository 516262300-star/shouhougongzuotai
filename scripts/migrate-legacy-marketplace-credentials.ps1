[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$LegacyRoot,
    [string]$EnvPath,
    [switch]$EnableTaobao,
    [switch]$EnableJd,
    [switch]$EnableDouyin
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

$serviceDir = Join-Path $LegacyRoot 'app\Services'
$taobaoPath = Join-Path $serviceDir 'TaobaoRefundService.php'
$jdPath = Join-Path $serviceDir 'JDRefundService.php'
$douyinPath = Join-Path $serviceDir 'DyRefundService.php'
foreach ($path in @($taobaoPath, $jdPath, $douyinPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "未找到旧服务文件：$path"
    }
}

function Get-RequiredMatchValue {
    param(
        [string]$Text,
        [string]$Pattern,
        [string]$Label
    )
    $match = [regex]::Match($Text, $Pattern)
    if (-not $match.Success -or [string]::IsNullOrWhiteSpace($match.Groups[1].Value)) {
        throw "旧源码中没有找到 $Label"
    }
    return $match.Groups[1].Value.Trim()
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

$taobao = Get-Content -LiteralPath $taobaoPath -Raw
$jd = Get-Content -LiteralPath $jdPath -Raw
$douyin = Get-Content -LiteralPath $douyinPath -Raw

$taobaoGateway = (Get-RequiredMatchValue $taobao 'private\s+\$gatewayUrl\s*=\s*["'']([^"'']+)["'']' '淘宝中转地址').TrimEnd('?') -replace '^http://', 'https://'
$taobaoShop = [ordered]@{
    shop_code = 'taobao-relay-01'
    shop_name = '淘宝（第三方中转）'
    platform_shop_id = 'taobao-relay-01'
    app_key = Get-RequiredMatchValue $taobao '\$this->appkey\s*=\s*["'']([^"'']+)["'']' '淘宝 app_key'
    app_secret = Get-RequiredMatchValue $taobao '\$clientSecret\s*=\s*["'']([^"'']+)["'']' '淘宝 app_secret'
    session_key = Get-RequiredMatchValue $taobao '\$sessionkey\s*=\s*["'']([^"'']+)["'']' '淘宝 session_key'
}

$jdGateway = (Get-RequiredMatchValue $jd 'private\s+\$gatewayUrl\s*=\s*["'']([^"'']+)["'']' '京东中转地址').TrimEnd('?') -replace '^http://', 'https://'
$jdFirstShop = [ordered]@{
    shop_code = 'jd-relay-01'
    shop_name = '京东一店（第三方中转）'
    platform_shop_id = Get-RequiredMatchValue $jd '\$store\s*=\s*["'']([^"'']+)["'']' '京东店铺代号'
    app_key = Get-RequiredMatchValue $jd '\$this->appkey\s*=\s*["'']([^"'']+)["'']' '京东 app_key'
    app_secret = Get-RequiredMatchValue $jd '\$clientSecret\s*=\s*["'']([^"'']+)["'']' '京东 app_secret'
    access_token = Get-RequiredMatchValue $jd '\$accessToken\s*=\s*["'']([^"'']+)["'']' '京东 access_token'
}
$jdSecondMatch = [regex]::Match(
    $jd,
    'if\s*\(\s*\$store\s*==\s*["'']p2-["'']\s*\)\s*\{(?<body>.*?)\}\s*elseif',
    [Text.RegularExpressions.RegexOptions]::Singleline
)
if (-not $jdSecondMatch.Success) {
    throw '旧源码中没有找到京东二店 set_key(p2-) 配置'
}
$jdSecondBlock = $jdSecondMatch.Groups['body'].Value
$jdSecondShop = [ordered]@{
    shop_code = 'jd-relay-02'
    shop_name = '京东二店（第三方中转）'
    platform_shop_id = 'p2-'
    app_key = Get-RequiredMatchValue $jdSecondBlock '\$this->appkey\s*=\s*["'']([^"'']+)["'']' '京东二店 app_key'
    app_secret = Get-RequiredMatchValue $jdSecondBlock '\$this->clientSecret\s*=\s*["'']([^"'']+)["'']' '京东二店 app_secret'
    access_token = Get-RequiredMatchValue $jdSecondBlock '\$this->accessToken\s*=\s*["'']([^"'']+)["'']' '京东二店 access_token'
}

$douyinShop = [ordered]@{
    shop_code = 'douyin-third-party-01'
    shop_name = '抖音（第三方应用授权）'
    platform_shop_id = Get-RequiredMatchValue $douyin '\$this->shop\s*=\s*["'']([^"'']+)["'']' '抖音店铺 ID'
    app_key = Get-RequiredMatchValue $douyin 'GlobalConfig::getGlobalConfig\(\)->appKey\s*=\s*["'']([^"'']+)["'']' '抖音 app_key'
    app_secret = Get-RequiredMatchValue $douyin 'GlobalConfig::getGlobalConfig\(\)->appSecret\s*=\s*["'']([^"'']+)["'']' '抖音 app_secret'
    access_token_mode = 'authorization_self'
}

$runtime = Join-Path $repoRoot '.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$backupPath = Join-Path $runtime ("env-before-third-party-marketplaces-{0}.bak" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Copy-Item -LiteralPath $resolvedEnvPath -Destination $backupPath

$content = [IO.File]::ReadAllText($resolvedEnvPath)
$updates = [ordered]@{
    TAOBAO_API_URL = $taobaoGateway
    TAOBAO_REQUEST_METHOD = 'GET'
    TAOBAO_SHOPS_JSON = (ConvertTo-Json @($taobaoShop) -Compress -Depth 4)
    JD_API_URL = $jdGateway
    JD_REQUEST_METHOD = 'GET'
    JD_SHOPS_JSON = (ConvertTo-Json @($jdFirstShop, $jdSecondShop) -Compress -Depth 4)
    DOUYIN_API_URL = 'https://openapi-fxg.jinritemai.com'
    DOUYIN_TOKEN_CACHE_PATH = '.runtime/douyin-access-token-cache.json'
    DOUYIN_TOKEN_REFRESH_SKEW_SECONDS = '300'
    DOUYIN_SHOPS_JSON = (ConvertTo-Json @($douyinShop) -Compress -Depth 4)
}
foreach ($entry in $updates.GetEnumerator()) {
    $content = Set-DotEnvValue $content $entry.Key ([string]$entry.Value)
}
if ($EnableTaobao) {
    $content = Set-DotEnvValue $content 'TAOBAO_SYNC_ENABLED' 'true'
}
if ($EnableJd) {
    $content = Set-DotEnvValue $content 'JD_SYNC_ENABLED' 'true'
}
if ($EnableDouyin) {
    $content = Set-DotEnvValue $content 'DOUYIN_SYNC_ENABLED' 'true'
}
[IO.File]::WriteAllText($resolvedEnvPath, $content, [Text.UTF8Encoding]::new($false))

Write-Output '已迁移：淘宝 1 店、京东 2 店、抖音 1 店。'
Write-Output "修改前备份：$backupPath"
$enabled = @()
if ($EnableTaobao) { $enabled += '淘宝' }
if ($EnableJd) { $enabled += '京东' }
if ($EnableDouyin) { $enabled += '抖音' }
if ($enabled.Count -gt 0) {
    Write-Output ("已开启常驻同步：" + ($enabled -join '、'))
} else {
    Write-Output '同步开关未自动开启；请先执行单窗口只读验证。'
}
