$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$serverDir = Split-Path -Parent $PSScriptRoot
Set-Location $serverDir

Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host "  小智 ESP32 · DeepSeek + 三层电脑操控系统 一键启动" -ForegroundColor Cyan
Write-Host "=========================================================" -ForegroundColor Cyan

# 1. 确保 Docker Desktop 已启动并就绪
$ver = docker info --format "{{.ServerVersion}}" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $ver) {
    Write-Host "[1/4] 检测到 Docker 未运行，正在自动启动 Docker Desktop..." -ForegroundColor Yellow
    Start-Service com.docker.service -ErrorAction SilentlyContinue
    if (Test-Path "E:\Docker\Docker Desktop.exe") {
        Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = '"E:\Docker\Docker Desktop.exe"' } | Out-Null
    } elseif (Test-Path "C:\Program Files\Docker\Docker\Docker Desktop.exe") {
        Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = '"C:\Program Files\Docker\Docker\Docker Desktop.exe"' } | Out-Null
    }
    for ($i = 0; $i -lt 35; $i++) {
        Start-Sleep -Seconds 2
        $ver = docker info --format "{{.ServerVersion}}" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) { break }
    }
}
Write-Host "[1/4] Docker 引擎已就绪 (v$ver)" -ForegroundColor Green

# 2. 启动本机三层电脑操控 MCP 服务 (端口 18900)
$mcpListening = Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue
if (-not $mcpListening) {
    Write-Host "[2/4] 正在后台启动三层电脑操控 MCP 服务 (端口 18900)..." -ForegroundColor Cyan
    $mcpPy = "$serverDir\pc-mcp\.venv\Scripts\python.exe"
    $mcpScript = "$serverDir\pc-mcp\pc_control_mcp.py"
    $mcpLog = "$serverDir\data\pc_mcp_18900.log"
    $mcpCmd = "cmd.exe /c `"`"$mcpPy`" `"$mcpScript`" > `"$mcpLog`" 2>&1`""
    $hideSi = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $mcpCmd; ProcessStartupInformation = $hideSi } | Out-Null
    for ($m = 0; $m -lt 10; $m++) {
        if (Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue) { break }
        Start-Sleep -Milliseconds 500
    }
} else {
    Write-Host "[2/4] 三层电脑操控 MCP 服务已在运行中 (端口 18900)" -ForegroundColor Green
}

# 3. 启动本机 5070 Ti GPT-SoVITS 语音合成服务 (端口 19880)
$gsvListening = Get-NetTCPConnection -LocalPort 19880 -State Listen -ErrorAction SilentlyContinue
if (-not $gsvListening) {
    Write-Host "[3/4] 正在后台启动本机 5070 Ti 语音合成引擎 (端口 19880)..." -ForegroundColor Cyan
    $gsvDir = "e:\故事\1\gpt-sovits-studio"
    $pyExe = "$gsvDir\.venv\Scripts\python.exe"
    $cfgPath = "$serverDir\data\tts_infer_xiaozhi.yaml"
    $logOut = "$serverDir\data\gsv_19880.log"
    $cmdLine = "cmd.exe /c `"cd /d `"$gsvDir`" && `"$pyExe`" api_v2.py -a 0.0.0.0 -p 19880 -c `"$cfgPath`" > `"$logOut`" 2>&1`""
    $hideGsv = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; ProcessStartupInformation = $hideGsv } | Out-Null
} else {
    Write-Host "[3/4] 本机 5070 Ti 语音合成引擎已在运行中" -ForegroundColor Green
}

# 4. 同步 DEEPSEEK_API_KEY 与 GEMINI_API_KEY 并启动双容器
Write-Host "[4/4] 正在同步环境变量并启动小智与记忆大脑容器..." -ForegroundColor Cyan
foreach ($envName in @("DEEPSEEK_API_KEY", "GEMINI_API_KEY")) {
    $val = [Environment]::GetEnvironmentVariable($envName, "User")
    if (-not $val) { $val = [Environment]::GetEnvironmentVariable($envName, "Machine") }
    if ($val) {
        Set-Item -Path "Env:$envName" -Value ($val.TrimStart([char]0xfeff).Trim())
    } else {
        Write-Host "[警告] 未在 Windows 环境变量中找到 $envName！" -ForegroundColor Yellow
    }
}

$animaImg = docker images -q xiaozhi-anima-brain:local 2>$null
if ($animaImg) {
    docker compose up -d
} else {
    docker compose up -d --build
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[错误] 容器启动失败，请检查上方报错信息！" -ForegroundColor Red
    exit 1
}

Write-Host "正在等待语音识别模型加载就绪 (约 10～15 秒)..." -ForegroundColor DarkGray
for ($j = 0; $j -lt 22; $j++) {
    try {
        $st = Invoke-RestMethod -Uri "http://localhost:18080/api/state" -TimeoutSec 2 -ErrorAction Stop
        if ($st.device.server_ready) { break }
    } catch {}
    Start-Sleep -Seconds 1
}

# 5. 等语音引擎加载完并预热一句后，再把启动期留下的冷内存页挪出物理内存
#    （必须在预热之后做：否则首句合成时才加载的模块会重新常驻内存）
try {
    for ($w = 0; $w -lt 90; $w++) {
        if (Get-NetTCPConnection -LocalPort 19880 -State Listen -ErrorAction SilentlyContinue) { break }
        Start-Sleep -Seconds 1
    }
    $warm = @{
        text = "预热。"; text_lang = "zh"; text_split_method = "cut0"; batch_size = 1; media_type = "wav"
        ref_audio_path = "e:/故事/1/xiaozhi-server/data/voices/raiden_mei/mei_default_ref.wav"
        prompt_text = "今夜月色很美，呵...适合与朋友小聚一番。"; prompt_lang = "zh"
    } | ConvertTo-Json -Compress
    Invoke-WebRequest -Uri "http://127.0.0.1:19880/tts" -Method Post -UseBasicParsing -TimeoutSec 60 `
        -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($warm)) | Out-Null
} catch {
    Write-Host "[提示] 语音引擎预热失败（不影响使用）: $($_.Exception.Message)" -ForegroundColor DarkYellow
}
try {
    Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class MemTrim { [DllImport("psapi.dll")] public static extern bool EmptyWorkingSet(IntPtr hProcess); }' -ErrorAction SilentlyContinue
    $gsvPids = Get-NetTCPConnection -LocalPort 19880 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($gp in $gsvPids) {
        $proc = Get-Process -Id $gp -ErrorAction SilentlyContinue
        if ($proc) { [void][MemTrim]::EmptyWorkingSet($proc.Handle) }
    }
    # WSL 自带的 autoMemoryReclaim 要等虚拟机空闲一段时间才触发，Docker 常驻时很少触发，所以启动后手动清一次文件缓存
    wsl -d docker-desktop -u root sh -c "sync; echo 3 > /proc/sys/vm/drop_caches" 2>$null
} catch {}

Write-Host "`n[启动成功] 全部服务已就绪（语音引擎已预热并回收冷内存）！" -ForegroundColor Green
Write-Host "  - 操控挡位 / API / 音色控制台: http://localhost:18080" -ForegroundColor White
Write-Host "  - 记忆大脑控制台:              http://localhost:18787" -ForegroundColor White
Write-Host "---------------------------------------------------------"
