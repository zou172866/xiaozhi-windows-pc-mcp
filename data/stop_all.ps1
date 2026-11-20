$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$serverDir = Split-Path -Parent $PSScriptRoot
Set-Location $serverDir

Write-Host "[1/5] 正在停止小智与记忆大脑 Docker 容器..." -ForegroundColor Cyan
$dockerInitiallyRunning = $false
$dockerVer = docker info --format "{{.ServerVersion}}" 2>$null
if ($LASTEXITCODE -eq 0 -and $dockerVer) {
    $dockerInitiallyRunning = $true
    docker compose stop
} else {
    Write-Host "  Docker 引擎未运行，跳过容器停止" -ForegroundColor DarkGray
}

Write-Host "[2/5] 正在关闭三层电脑操控 MCP 服务 (端口 18900)..." -ForegroundColor Cyan
$mcpPids = Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($mp in $mcpPids) {
    Stop-Process -Id $mp -Force -ErrorAction SilentlyContinue
    Write-Host "  已关闭电脑操控进程 PID=$mp" -ForegroundColor DarkGray
}

Write-Host "[3/5] 正在关闭本地 19880 语音合成进程并释放显卡显存..." -ForegroundColor Cyan
$pids = Get-NetTCPConnection -LocalPort 19880 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $pids) {
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    Write-Host "  已关闭语音合成进程 PID=$p" -ForegroundColor DarkGray
}

Write-Host "[4/5] 正在检查并清理残留的 Antigravity Sidecar 桥接进程..." -ForegroundColor Cyan
$sidecarStopped = 0
try {
    $sidecarProcs = Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^python(w)?(\.exe)?$' -and
            $_.CommandLine -and
            $_.CommandLine -like '*agy_sidecar_worker.py*'
        }
    foreach ($sp in $sidecarProcs) {
        Stop-Process -Id $sp.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  已清理残留 Sidecar 进程 PID=$($sp.ProcessId)" -ForegroundColor DarkGray
        $sidecarStopped++
    }
} catch {}
$hbFile = Join-Path $serverDir "data\agy_tasks\worker_heartbeat.json"
if ($sidecarStopped -gt 0 -and (Test-Path $hbFile)) {
    Remove-Item -Path $hbFile -Force -ErrorAction SilentlyContinue
}

Write-Host "[5/5] 正在检查是否可自动退出 Docker Desktop 并释放 Vmmem 内存..." -ForegroundColor Cyan
$otherContainers = @()
if ($dockerInitiallyRunning) {
    $otherContainers = @(docker ps --format "{{.Names}}" 2>$null | Where-Object { $_ -and $_.Trim() })
}

if ($otherContainers.Count -gt 0) {
    Write-Host "  检测到仍有其他 Docker 容器运行中 ($($otherContainers -join ', '))，已保留 Docker Desktop 与 Vmmem" -ForegroundColor Yellow
} else {
    $dockerProcNames = @("Docker Desktop", "com.docker.backend", "com.docker.build", "com.docker.dev-envs")
    $hasDockerProcs = @(Get-Process -Name $dockerProcNames -ErrorAction SilentlyContinue).Count -gt 0
    if ($dockerInitiallyRunning -or $hasDockerProcs) {
        Write-Host "  无其他运行中容器，正在优雅退出 Docker Desktop..." -ForegroundColor DarkGray
        docker desktop stop --timeout 25 2>$null | Out-Null
        for ($w = 0; $w -lt 8; $w++) {
            $rem = @(Get-Process -Name $dockerProcNames -ErrorAction SilentlyContinue)
            if ($rem.Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
        $rem = @(Get-Process -Name $dockerProcNames -ErrorAction SilentlyContinue)
        if ($rem.Count -gt 0) {
            foreach ($rp in $rem) {
                Stop-Process -Id $rp.Id -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds 1
        }
        Write-Host "  Docker Desktop 已退出" -ForegroundColor DarkGray
    } else {
        Write-Host "  Docker Desktop 未运行" -ForegroundColor DarkGray
    }

    $runningWsl = @()
    try {
        $rawWsl = wsl --list --running --quiet 2>$null
        foreach ($ln in $rawWsl) {
            $name = ($ln -replace "`0", "").Trim()
            if ($name) { $runningWsl += $name }
        }
    } catch {}

    $nonDockerWsl = @($runningWsl | Where-Object { $_ -notin @("docker-desktop", "docker-desktop-data") })
    if ($nonDockerWsl.Count -gt 0) {
        foreach ($dd in @("docker-desktop", "docker-desktop-data")) {
            if ($runningWsl -contains $dd) {
                wsl --terminate $dd 2>$null | Out-Null
            }
        }
        Write-Host "  检测到其他 WSL 实例仍在运行 ($($nonDockerWsl -join ', '))，仅终止 docker-desktop（保留 Vmmem）" -ForegroundColor Yellow
    } else {
        $hasVmmem = @(Get-Process -Name "vmmem*" -ErrorAction SilentlyContinue).Count -gt 0
        if ($runningWsl.Count -gt 0 -or $hasVmmem) {
            Write-Host "  正在执行 wsl --shutdown 彻底释放 Vmmem 内存..." -ForegroundColor DarkGray
            wsl --shutdown 2>$null | Out-Null
            for ($v = 0; $v -lt 12; $v++) {
                if (@(Get-Process -Name "vmmem*" -ErrorAction SilentlyContinue).Count -eq 0) { break }
                Start-Sleep -Milliseconds 500
            }
            if (@(Get-Process -Name "vmmem*" -ErrorAction SilentlyContinue).Count -eq 0) {
                Write-Host "  Vmmem 已彻底退出并释放全部虚拟机内存" -ForegroundColor Green
            } else {
                Write-Host "  已发送 WSL 关闭指令，Vmmem 正在后台收尾释放中" -ForegroundColor DarkYellow
            }
        } else {
            Write-Host "  Vmmem 未运行，无需释放" -ForegroundColor DarkGray
        }
    }
}

Write-Host "`n[已关闭] 小智全部后台服务、显存与闲置虚拟机内存已彻底释放！" -ForegroundColor Green
