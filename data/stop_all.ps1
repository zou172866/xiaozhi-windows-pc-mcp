$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$serverDir = Split-Path -Parent $PSScriptRoot
Set-Location $serverDir

Write-Host "[1/3] 正在停止小智与记忆大脑 Docker 容器..." -ForegroundColor Cyan
docker compose stop

Write-Host "[2/3] 正在关闭三层电脑操控 MCP 服务 (端口 18900)..." -ForegroundColor Cyan
$mcpPids = Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($mp in $mcpPids) {
    Stop-Process -Id $mp -Force -ErrorAction SilentlyContinue
    Write-Host "  已关闭电脑操控进程 PID=$mp" -ForegroundColor DarkGray
}

Write-Host "[3/3] 正在关闭本地 19880 语音合成进程并释放显卡显存..." -ForegroundColor Cyan
$pids = Get-NetTCPConnection -LocalPort 19880 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $pids) {
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    Write-Host "  已关闭语音合成进程 PID=$p" -ForegroundColor DarkGray
}

Write-Host "`n[已关闭] 小智全部后台服务与显存已彻底释放！" -ForegroundColor Green
