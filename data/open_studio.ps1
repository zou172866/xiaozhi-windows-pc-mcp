$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$serverDir = Split-Path -Parent $PSScriptRoot
$urlFile = Join-Path $serverDir "data\anima\studio_url.txt"

if (-not (Test-Path $urlFile)) {
    Write-Host "[提示] 尚未检测到 Companion Studio 访问令牌，请先确认 Docker 服务已启动！" -ForegroundColor Yellow
    exit 1
}

$rawContent = [System.IO.File]::ReadAllText($urlFile, [System.Text.Encoding]::UTF8)
$firstLine = ($rawContent -split "\r?\n")[0]
$studioUrl = if ($firstLine) { $firstLine.TrimStart([char]0xfeff).Trim() } else { "" }
if (-not $studioUrl) {
    Write-Host "[提示] Companion Studio 访问地址为空，请重启 xiaozhi-anima-brain 容器后重试！" -ForegroundColor Yellow
    exit 1
}

Write-Host "正在用默认浏览器打开 Companion Studio 网页控制台..." -ForegroundColor Cyan
Start-Process $studioUrl
