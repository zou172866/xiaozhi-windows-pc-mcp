$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$serverDir = Split-Path -Parent $PSScriptRoot
Set-Location $serverDir

Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host "  小智 ESP32 · 三层电脑操控与长期记忆系统 一键启动" -ForegroundColor Cyan
Write-Host "=========================================================" -ForegroundColor Cyan

# 0. 确保模型目录存在，加载本地 .env，并自动推导宿主机路径与局域网 IP
foreach ($relDir in @("models\sherpa-onnx-sense-voice", "models\SenseVoiceSmall", "data\anima", "data\agy_tasks\pending", "data\agy_tasks\active")) {
    $fullD = Join-Path $serverDir $relDir
    if (-not (Test-Path $fullD)) { New-Item -ItemType Directory -Path $fullD -Force | Out-Null }
}

$runtimeTemplates = @(
    @{ Tpl = "data\.config.example.yaml";             Dst = "data\.config.yaml" },
    @{ Tpl = "data\pc_control_mode.example.json";     Dst = "data\pc_control_mode.json" },
    @{ Tpl = "data\audio_output_mode.example.json";   Dst = "data\audio_output_mode.json" },
    @{ Tpl = "data\reply_length_config.example.json"; Dst = "data\reply_length_config.json" },
    @{ Tpl = "data\tts_infer_xiaozhi.example.yaml";   Dst = "data\tts_infer_xiaozhi.yaml" },
    @{ Tpl = "data\.wakeup_words.example.yaml";       Dst = "data\.wakeup_words.yaml" }
)
foreach ($rt in $runtimeTemplates) {
    $tplP = Join-Path $serverDir $rt.Tpl
    $dstP = Join-Path $serverDir $rt.Dst
    if (-not (Test-Path $dstP) -and (Test-Path $tplP)) {
        Copy-Item $tplP $dstP
        Write-Host "[初始化] 已从 $($rt.Tpl) 生成默认运行文件 $($rt.Dst)" -ForegroundColor Yellow
    }
}

$dotEnvPath = Join-Path $serverDir ".env"
$explicitEmptyEnvKeys = @{}
if (Test-Path $dotEnvPath) {
    foreach ($rawLine in (Get-Content -Path $dotEnvPath -Encoding UTF8)) {
        $line = $rawLine.TrimStart([char]0xfeff).Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $idx = $line.IndexOf("=")
        if ($idx -le 0) { continue }
        $k = $line.Substring(0, $idx).Trim()
        $v = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
        if (-not $k) { continue }
        if ($v -eq "") {
            # Explicit `KEY=` in .env must take precedence over stale User/Process env for docker compose
            $explicitEmptyEnvKeys[$k] = $true
            Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
        } elseif (-not [Environment]::GetEnvironmentVariable($k, "Process")) {
            Set-Item -Path "Env:$k" -Value $v
        }
    }
}

foreach ($envName in @("DEEPSEEK_API_KEY", "GEMINI_API_KEY", "ANIMA_LLM_API_KEY", "ANIMA_SUMMARY_API_KEY", "ANIMA_EMBEDDING_API_KEY")) {
    if ($explicitEmptyEnvKeys.ContainsKey($envName)) { continue }
    $val = [Environment]::GetEnvironmentVariable($envName, "User")
    if (-not $val) { $val = [Environment]::GetEnvironmentVariable($envName, "Machine") }
    if ($val) {
        Set-Item -Path "Env:$envName" -Value ($val.TrimStart([char]0xfeff).Trim())
    }
}

$env:HOST_DATA_DIR = (Join-Path $serverDir "data").Replace("\", "/")
if (-not $env:HOST_LAN_IP) {
    try {
        $lanIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*" -and
                $_.InterfaceAlias -notmatch "WSL|vEthernet|Docker|Loopback|Hyper-V|VirtualBox|VMware"
            } |
            Sort-Object -Property InterfaceMetric |
            Select-Object -First 1 -ExpandProperty IPAddress
        if ($lanIp) { $env:HOST_LAN_IP = $lanIp }
    } catch {}
}
if (-not $env:HOST_LAN_IP) { $env:HOST_LAN_IP = "127.0.0.1" }
Write-Host "[环境] 局域网 IP: $($env:HOST_LAN_IP) | 数据目录: $($env:HOST_DATA_DIR)" -ForegroundColor DarkGray

# 1. 确保 Docker Desktop 已启动并就绪
$ver = docker info --format "{{.ServerVersion}}" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $ver) {
    Write-Host "[1/4] 检测到 Docker 未运行，正在自动启动 Docker Desktop..." -ForegroundColor Yellow
    Start-Service com.docker.service -ErrorAction SilentlyContinue
    $dockerCandidates = @(
        "C:\Program Files\Docker\Docker\Docker Desktop.exe",
        "E:\Docker\Docker Desktop.exe",
        "D:\Docker\Docker Desktop.exe"
    )
    foreach ($dc in $dockerCandidates) {
        if (Test-Path $dc) {
            Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "`"$dc`"" } | Out-Null
            break
        }
    }
    for ($i = 0; $i -lt 35; $i++) {
        Start-Sleep -Seconds 2
        $ver = docker info --format "{{.ServerVersion}}" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) { break }
    }
}
Write-Host "[1/4] Docker 引擎已就绪 (v$ver)" -ForegroundColor Green

# 2. 启动本机三层电脑操控 MCP 服务 (端口 18900，仅绑定 127.0.0.1 + 自动 Bearer Token + 日志轮转)
$mcpListening = Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue
if (-not $mcpListening) {
    Write-Host "[2/4] 正在后台启动三层电脑操控 MCP + 电脑音响直通服务 (127.0.0.1:18900)..." -ForegroundColor Cyan
    $mcpPy = Join-Path $serverDir "pc-mcp\.venv\Scripts\python.exe"
    if (-not (Test-Path $mcpPy)) { $mcpPy = "python" }
    $mcpScript = Join-Path $serverDir "pc-mcp\pc_control_mcp.py"
    $mcpBootLog = Join-Path $serverDir "data\pc_mcp_18900_boot.log"
    $mcpCmd = "cmd.exe /c `"set PYTHONUTF8=1&& `"$mcpPy`" -X utf8 `"$mcpScript`" > `"$mcpBootLog`" 2>&1`""
    $hideSi = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $mcpCmd; ProcessStartupInformation = $hideSi } | Out-Null
    for ($m = 0; $m -lt 14; $m++) {
        if (Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue) { break }
        Start-Sleep -Milliseconds 500
    }
} else {
    Write-Host "[2/4] 三层电脑操控 MCP 服务已在运行中 (端口 18900)" -ForegroundColor Green
}

# 3. 可选：启动本机 GPU GPT-SoVITS 语音合成服务 (127.0.0.1:19880)；无独显/未安装则自动优雅跳过并走 EdgeTTS
$gsvEnabled = $true
if ($env:ENABLE_LOCAL_GSV -and $env:ENABLE_LOCAL_GSV.ToLower() -in @("false", "0", "no", "off")) {
    $gsvEnabled = $false
}
$gsvDir = if ($env:GSV_DIR) { $env:GSV_DIR } else { Join-Path (Split-Path -Parent $serverDir) "gpt-sovits-studio" }
$gsvPy = Join-Path $gsvDir ".venv\Scripts\python.exe"
$gsvApi = Join-Path $gsvDir "api_v2.py"
$hasLocalGsv = $gsvEnabled -and (Test-Path $gsvPy) -and (Test-Path $gsvApi)

if ($hasLocalGsv) {
    $gsvListening = Get-NetTCPConnection -LocalPort 19880 -State Listen -ErrorAction SilentlyContinue
    if (-not $gsvListening) {
        Write-Host "[3/4] 正在后台启动本机 GPU GPT-SoVITS 语音引擎 (127.0.0.1:19880)..." -ForegroundColor Cyan
        $cfgPath = Join-Path $serverDir "data\tts_infer_xiaozhi.yaml"
        $logOut = Join-Path $serverDir "data\gsv_19880.log"
        $cmdLine = "cmd.exe /c `"cd /d `"$gsvDir`" && set PYTHONUTF8=1&& set GSV_IDLE_TRIM_SECONDS=60&& `"$gsvPy`" -X utf8 api_v2.py -a 127.0.0.1 -p 19880 -c `"$cfgPath`" > `"$logOut`" 2>&1`""
        $hideGsv = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
        Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; ProcessStartupInformation = $hideGsv } | Out-Null
    } else {
        Write-Host "[3/4] 本机 GPU GPT-SoVITS 语音引擎已在运行中" -ForegroundColor Green
    }
} else {
    Write-Host "[3/4] 未启用本地 GPU GPT-SoVITS（无独显/免显存模式，将使用内置 EdgeTTS）" -ForegroundColor DarkGreen
}

# 4. 启动小智与 Anima Brain 双容器
Write-Host "[4/4] 正在启动小智主服务与长期记忆大脑容器..." -ForegroundColor Cyan
if (-not $env:DEEPSEEK_API_KEY -and -not $env:GEMINI_API_KEY) {
    Write-Host "[警告] 未检测到 DEEPSEEK_API_KEY 或 GEMINI_API_KEY 环境变量，请先配置！" -ForegroundColor Yellow
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

Write-Host "正在等待语音识别模型加载就绪 (约 5～15 秒)..." -ForegroundColor DarkGray
for ($j = 0; $j -lt 22; $j++) {
    try {
        $st = Invoke-RestMethod -Uri "http://localhost:18080/api/state" -TimeoutSec 2 -ErrorAction Stop
        if ($st.device.server_ready) { break }
    } catch {}
    Start-Sleep -Seconds 1
}

# 5. 若启用了本地 GPU 语音引擎，则预热一句并裁剪冷内存页
if ($hasLocalGsv) {
    $warmRef = Join-Path $serverDir "data\voices\raiden_mei\mei_default_ref.wav"
    if (Test-Path $warmRef) {
        try {
            for ($w = 0; $w -lt 90; $w++) {
                if (Get-NetTCPConnection -LocalPort 19880 -State Listen -ErrorAction SilentlyContinue) { break }
                Start-Sleep -Seconds 1
            }
            $warm = @{
                text = "预热测试，银行行长今天很开心。"; text_lang = "zh"; text_split_method = "cut0"; batch_size = 1; media_type = "wav"; sample_steps = 16
                ref_audio_path = $warmRef.Replace("\", "/")
                prompt_text = "今夜月色很美，呵...适合与朋友小聚一番。"; prompt_lang = "zh"
            } | ConvertTo-Json -Compress
            Invoke-WebRequest -Uri "http://127.0.0.1:19880/tts" -Method Post -UseBasicParsing -TimeoutSec 60 `
                -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($warm)) | Out-Null
        } catch {
            Write-Host "[提示] 语音引擎预热失败（不影响使用）: $($_.Exception.Message)" -ForegroundColor DarkYellow
        }
    }
}

try {
    Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class MemTrim { [DllImport("psapi.dll")] public static extern bool EmptyWorkingSet(IntPtr hProcess); }' -ErrorAction SilentlyContinue
    $trimPids = Get-NetTCPConnection -LocalPort 19880, 18900 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($gp in $trimPids) {
        $proc = Get-Process -Id $gp -ErrorAction SilentlyContinue
        if ($proc) { [void][MemTrim]::EmptyWorkingSet($proc.Handle) }
    }
    wsl -d docker-desktop -u root sh -c "sync; echo 3 > /proc/sys/vm/drop_caches" 2>$null
} catch {}

Write-Host "`n[启动成功] 全部服务已就绪！" -ForegroundColor Green
Write-Host "  - 主控制台 (挡位 / L3引擎 / 音响 / 字数 / 亲密度): http://localhost:18080" -ForegroundColor White
Write-Host "  - 记忆大脑控制台:                                  http://localhost:18787" -ForegroundColor White
Write-Host "  - ESP32 OTA 地址:                                  http://$($env:HOST_LAN_IP):18003/xiaozhi/ota/" -ForegroundColor White
Write-Host "---------------------------------------------------------"
