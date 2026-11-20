$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$serverDir = Split-Path -Parent $PSScriptRoot
Set-Location $serverDir

Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host "  小智 ESP32 · 三层电脑操控与长期记忆系统 一键启动" -ForegroundColor Cyan
Write-Host "=========================================================" -ForegroundColor Cyan

# 0. 确保拆分后的目录结构与宿主机私有状态目录存在（A-1 / A-2），并自动兼容迁移旧 data/ 路径
foreach ($relDir in @(
    "models\sherpa-onnx-sense-voice",
    "models\SenseVoiceSmall",
    "models\gsv_weights",
    "config\runtime",
    "config\runtime\bin",
    "state",
    "secrets",
    "backups",
    "voices",
    "voices\raiden_mei",
    "logs",
    "anima-data",
    "data\obsidian_export",
    "pc-mcp\state",
    "pc-mcp\state\agy_tasks\pending",
    "pc-mcp\state\agy_tasks\active"
)) {
    $fullD = Join-Path $serverDir $relDir
    if (-not (Test-Path $fullD)) { New-Item -ItemType Directory -Path $fullD -Force | Out-Null }
}

# 自动无损兼容迁移旧 data/ 文件至新职责目录（仅当目标不存在时迁移，便于灰度回滚）
$legacyFileMoves = @(
    @{ Src = "data\.config.yaml";              Dst = "config\runtime\.config.yaml" },
    @{ Src = "data\tts_infer_xiaozhi.yaml";    Dst = "config\runtime\tts_infer_xiaozhi.yaml" },
    @{ Src = "data\.wakeup_words.yaml";        Dst = "config\runtime\.wakeup_words.yaml" },
    @{ Src = "data\runtime_settings.json";     Dst = "state\runtime_settings.json" },
    @{ Src = "data\chat_history.json";         Dst = "state\chat_history.json" },
    @{ Src = "data\.console_log_cursor.json";  Dst = "state\.console_log_cursor.json" },
    @{ Src = "data\pc_mcp_token.txt";          Dst = "secrets\pc_mcp_token.txt" }
)
foreach ($mv in $legacyFileMoves) {
    $srcP = Join-Path $serverDir $mv.Src
    $dstP = Join-Path $serverDir $mv.Dst
    if ((Test-Path $srcP) -and -not (Test-Path $dstP)) {
        try {
            Copy-Item -Path $srcP -Destination $dstP -Force
            Write-Host "[迁移] 已兼容迁移 $($mv.Src) -> $($mv.Dst)" -ForegroundColor DarkCyan
        } catch {}
    }
}

$runtimeTemplates = @(
    @{ Tpl = "config\.config.example.yaml";             Dst = "config\runtime\.config.yaml" },
    @{ Tpl = "config\tts_infer_xiaozhi.example.yaml";   Dst = "config\runtime\tts_infer_xiaozhi.yaml" },
    @{ Tpl = "config\.wakeup_words.example.yaml";       Dst = "config\runtime\.wakeup_words.yaml" }
)
foreach ($rt in $runtimeTemplates) {
    $tplP = Join-Path $serverDir $rt.Tpl
    $dstP = Join-Path $serverDir $rt.Dst
    if (-not (Test-Path $dstP) -and (Test-Path $tplP)) {
        Copy-Item $tplP $dstP
        Write-Host "[初始化] 已从 $($rt.Tpl) 生成默认运行文件 $($rt.Dst)" -ForegroundColor Yellow
    }
}
$rsDst = Join-Path $serverDir "state\runtime_settings.json"
$rsTpl = Join-Path $serverDir "config\runtime_settings.example.json"
$hasLegacyRs = (Test-Path (Join-Path $serverDir "state\pc_control_mode.json")) -or
               (Test-Path (Join-Path $serverDir "state\audio_output_mode.json")) -or
               (Test-Path (Join-Path $serverDir "state\reply_length_config.json"))
if (-not (Test-Path $rsDst) -and -not $hasLegacyRs -and (Test-Path $rsTpl)) {
    Copy-Item $rsTpl $rsDst
    Write-Host "[初始化] 已从 config\runtime_settings.example.json 生成默认统一运行配置 state\runtime_settings.json" -ForegroundColor Yellow
}
# A-2: 宿主机私有挡位文件默认初始化为 safe（绝不信任容器可写目录的挡位）
$hostModeFile = Join-Path $serverDir "pc-mcp\state\pc_control_mode.json"
if (-not (Test-Path $hostModeFile)) {
    [System.IO.File]::WriteAllText($hostModeFile, '{"mode": "safe"}' + "`r`n", [System.Text.UTF8Encoding]::new($false))
    Write-Host "[安全] 已初始化宿主机私有操控挡位 pc-mcp\state\pc_control_mode.json (mode=safe)" -ForegroundColor Green
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

$env:HOST_PROJECT_DIR = $serverDir.Replace("\", "/")
$env:HOST_DATA_DIR = (Join-Path $serverDir "data").Replace("\", "/")
$env:HOST_VOICES_DIR = (Join-Path $serverDir "voices").Replace("\", "/")
$env:HOST_GSV_WEIGHTS_DIR = (Join-Path $serverDir "models\gsv_weights").Replace("\", "/")
if (-not $env:ESP32_AUTH_KEY) {
    try {
        $randBytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($randBytes)
        $rng.Dispose()
        $genKey = ([System.BitConverter]::ToString($randBytes)).Replace("-", "").ToLowerInvariant()
        $env:ESP32_AUTH_KEY = $genKey
        if (Test-Path $dotEnvPath) {
            $curEnvText = [System.IO.File]::ReadAllText($dotEnvPath, [System.Text.Encoding]::UTF8)
            if ($curEnvText -notmatch '(?m)^\s*ESP32_AUTH_KEY\s*=') {
                $sep = if ($curEnvText.EndsWith("`n")) { "" } else { "`r`n" }
                [System.IO.File]::AppendAllText($dotEnvPath, "${sep}ESP32_AUTH_KEY=$genKey`r`n", [System.Text.UTF8Encoding]::new($false))
            }
        } else {
            [System.IO.File]::WriteAllText($dotEnvPath, "ESP32_AUTH_KEY=$genKey`r`n", [System.Text.UTF8Encoding]::new($false))
        }
        Write-Host "[安全] 已自动生成 ESP32 通道签名密钥并写入本地 .env（已被 .gitignore 隔离）" -ForegroundColor Green
    } catch {
        Write-Host "[提示] 自动生成 ESP32_AUTH_KEY 失败，将由容器内存随机生成: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
if (-not $env:ESP32_ALLOWED_DEVICES) {
    Write-Host "[提示] .env 中尚未配置 ESP32_ALLOWED_DEVICES（设备 MAC 白名单），开启鉴权后需填入 ESP32 MAC 才能接入 18000/18003。" -ForegroundColor Yellow
}
if ($env:OBSIDIAN_EXPORT_DIR -and -not (Test-Path $env:OBSIDIAN_EXPORT_DIR)) {
    try { New-Item -ItemType Directory -Path $env:OBSIDIAN_EXPORT_DIR -Force | Out-Null } catch {}
}
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

# 2. 启动或重启本机三层电脑操控 MCP 服务 (端口 18900，仅绑定 127.0.0.1 + 自动 Bearer Token + 看门狗守护)
$mcpPy = Join-Path $serverDir "pc-mcp\.venv\Scripts\python.exe"
if (-not (Test-Path $mcpPy)) { $mcpPy = "python" }
$mcpScript = Join-Path $serverDir "pc-mcp\pc_control_mcp.py"
$mcpWatchdogScript = Join-Path $serverDir "pc-mcp\pc_mcp_watchdog.py"
$mcpBootLog = Join-Path $serverDir "logs\pc_mcp_18900_boot.log"
$mcpRuntimeLog = Join-Path $serverDir "logs\pc_mcp_18900.log"
$mcpTokenFile = Join-Path $serverDir "secrets\pc_mcp_token.txt"
$watchdogStopFlag = Join-Path $serverDir "pc-mcp\state\watchdog_stop.flag"

function Test-PcMcpAlive {
    param([string]$TokenPath)
    if (-not (Test-Path $TokenPath)) { return $false }
    try {
        $tok = (Get-Content -Path $TokenPath -Raw -Encoding UTF8 -ErrorAction Stop).Trim()
        if (-not $tok) { return $false }
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:18900/audio/status" `
            -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 2 -ErrorAction Stop
        return ($null -ne $resp -and $resp.ok -eq $true)
    } catch {
        return $false
    }
}

# 先停止旧看门狗，避免重启 18900 过程中并发拉起
try {
    New-Item -ItemType File -Path $watchdogStopFlag -Force -ErrorAction SilentlyContinue | Out-Null
    $oldWatchdogs = Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^python(w)?(\.exe)?$' -and $_.CommandLine -and $_.CommandLine -like '*pc_mcp_watchdog.py*' }
    foreach ($ow in $oldWatchdogs) {
        Stop-Process -Id $ow.ProcessId -Force -ErrorAction SilentlyContinue
    }
} catch {}

$mcpListening = Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue
$oldMcpPidSet = @{}
if ($mcpListening) {
    foreach ($omp in ($mcpListening | Select-Object -ExpandProperty OwningProcess -Unique)) {
        if ($omp) { $oldMcpPidSet[[int]$omp] = $true }
    }
}
try {
    $oldMcpPyProcs = Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^python(w)?(\.exe)?$' -and $_.CommandLine -and $_.CommandLine -like '*pc_control_mcp.py*' }
    foreach ($op in $oldMcpPyProcs) {
        if ($op.ProcessId) { $oldMcpPidSet[[int]$op.ProcessId] = $true }
    }
} catch {}

if ($mcpListening -or $oldMcpPidSet.Count -gt 0) {
    $wasAlive = Test-PcMcpAlive -TokenPath $mcpTokenFile
    if ($wasAlive) {
        Write-Host "[2/4] 检测到旧 18900 MCP 服务在线，正在平滑重启以加载最新代码与配置..." -ForegroundColor Cyan
    } else {
        Write-Host "[2/4] 检测到旧 18900 MCP 进程但 /audio/status 验活失败，正在强制重启..." -ForegroundColor Yellow
    }
    if (Test-Path $mcpWatchdogScript) {
        try { & $mcpPy -X utf8 $mcpWatchdogScript --cleanup-l3 2>$null | Out-Null } catch {}
    }
    foreach ($omp in $oldMcpPidSet.Keys) {
        Stop-Process -Id $omp -Force -ErrorAction SilentlyContinue
    }
    for ($w = 0; $w -lt 12; $w++) {
        if (-not (Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
} else {
    Write-Host "[2/4] 正在后台启动三层电脑操控 MCP + 电脑音响直通服务 (127.0.0.1:18900)..." -ForegroundColor Cyan
}

$mcpCmd = "cmd.exe /c `"set PYTHONUTF8=1&& `"$mcpPy`" -X utf8 `"$mcpScript`" > `"$mcpBootLog`" 2>&1`""
$hideSi = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $mcpCmd; ProcessStartupInformation = $hideSi } | Out-Null

$mcpHealthy = $false
for ($m = 0; $m -lt 24; $m++) {
    if (Test-PcMcpAlive -TokenPath $mcpTokenFile) {
        $mcpHealthy = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if ($mcpHealthy) {
    Write-Host "[2/4] 三层电脑操控 MCP 服务已就绪并通过 /audio/status 鉴权验活 (127.0.0.1:18900)" -ForegroundColor Green
    Remove-Item -Path $watchdogStopFlag -Force -ErrorAction SilentlyContinue
    if (Test-Path $mcpWatchdogScript) {
        $wdCmd = "cmd.exe /c `"set PYTHONUTF8=1&& `"$mcpPy`" -X utf8 `"$mcpWatchdogScript`" --port 18900 --interval 8`""
        $hideWd = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
        Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $wdCmd; ProcessStartupInformation = $hideWd } | Out-Null
        Write-Host "[2/4] 已启动宿主机 18900 无黑框看门狗守护进程 (pc_mcp_watchdog.py)" -ForegroundColor DarkGreen
    }
} else {
    Write-Host "[警告] 宿主机 18900 MCP 服务未通过 /audio/status 鉴权验活！" -ForegroundColor Red
    $tailSrc = if ((Test-Path $mcpBootLog) -and ((Get-Item $mcpBootLog).Length -gt 0)) { $mcpBootLog } else { $mcpRuntimeLog }
    if (Test-Path $tailSrc) {
        Write-Host "----- $tailSrc (最后 40 行) -----" -ForegroundColor Yellow
        Get-Content -Path $tailSrc -Tail 40 -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkYellow }
        Write-Host "----------------------------------" -ForegroundColor Yellow
    } else {
        Write-Host "  未找到启动日志文件: $mcpBootLog" -ForegroundColor DarkYellow
    }
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
        $cfgPath = Join-Path $serverDir "config\runtime\tts_infer_xiaozhi.yaml"
        $logOut = Join-Path $serverDir "logs\gsv_19880.log"
        $gsvBootLog = Join-Path $serverDir "logs\gsv_19880_boot.log"
        # H-6: 启动前按 5 MB x 3 份轮转旧日志，进程内由 api_v2.py RotatingFileHandler 持续控体积（避免 cmd > 锁句柄阻塞轮转）
        try {
            $gsvMaxBytes = 5 * 1024 * 1024
            $gsvBackups = 3
            if ((Test-Path $logOut) -and ((Get-Item $logOut).Length -ge $gsvMaxBytes)) {
                for ($bi = $gsvBackups; $bi -ge 1; $bi--) {
                    $srcB = if ($bi -eq 1) { $logOut } else { "$logOut.$($bi - 1)" }
                    $dstB = "$logOut.$bi"
                    if (Test-Path $srcB) {
                        Move-Item -Path $srcB -Destination $dstB -Force -ErrorAction SilentlyContinue
                    }
                }
            }
        } catch {}
        # F-3 / H-6: 空闲回收放宽至 300s 且按系统内存压力 (>=75%) 触发；日志通过 --log_file 走进程内 5 MB x 3 轮转
        $cmdLine = "cmd.exe /c `"cd /d `"$gsvDir`" && set PYTHONUTF8=1&& set GSV_IDLE_TRIM_SECONDS=300&& set GSV_IDLE_TRIM_MEM_THRESHOLD=75&& `"$gsvPy`" -X utf8 api_v2.py -a 127.0.0.1 -p 19880 -c `"$cfgPath`" --log_file `"$logOut`" --log_max_bytes 5242880 --log_backup_count 3 > `"$gsvBootLog`" 2>&1`""
        $hideGsv = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
        Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; ProcessStartupInformation = $hideGsv } | Out-Null
    } else {
        Write-Host "[3/4] 本机 GPU GPT-SoVITS 语音引擎已在运行中" -ForegroundColor Green
    }
} else {
    Write-Host "[3/4] 未启用本地 GPU GPT-SoVITS（无独显/免显存模式，将使用内置 EdgeTTS）" -ForegroundColor DarkGreen
}

# A-1: 按最小权限拆分宿主机私有令牌至 secrets\anima_tokens.env 与 secrets\pc_mcp_tokens.env
#      anima-brain 仅接收 anima_tokens.env（严禁暴露 PC_MCP_TOKEN），主容器按需接收两个 env_file
try {
    $animaTokFile = Join-Path $serverDir "secrets\anima_token.txt"
    $animaDataTok = Join-Path $serverDir "anima-data\studio_token.txt"
    $animaTok = ""
    if (Test-Path $animaDataTok) {
        $animaTok = (Get-Content -Path $animaDataTok -Raw -Encoding UTF8 -ErrorAction SilentlyContinue).Trim()
    }
    if (-not $animaTok -and (Test-Path $animaTokFile)) {
        $animaTok = (Get-Content -Path $animaTokFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue).Trim()
    }
    if (-not $animaTok -or $animaTok.Length -lt 24) {
        $randB = New-Object byte[] 32
        $rngA = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rngA.GetBytes($randB)
        $rngA.Dispose()
        $animaTok = [Convert]::ToBase64String($randB).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    }
    [System.IO.File]::WriteAllText($animaTokFile, $animaTok + "`n", [System.Text.UTF8Encoding]::new($false))

    $pcTok = ""
    if (Test-Path $mcpTokenFile) {
        $pcTok = (Get-Content -Path $mcpTokenFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue).Trim()
    }
    $animaEnvFile = Join-Path $serverDir "secrets\anima_tokens.env"
    $pcMcpEnvFile = Join-Path $serverDir "secrets\pc_mcp_tokens.env"
    if ($animaTok) {
        [System.IO.File]::WriteAllText($animaEnvFile, "ANIMA_STUDIO_TOKEN=$animaTok`n", [System.Text.UTF8Encoding]::new($false))
    }
    if ($pcTok) {
        [System.IO.File]::WriteAllText($pcMcpEnvFile, "PC_MCP_TOKEN=$pcTok`n", [System.Text.UTF8Encoding]::new($false))
    }
    $legacyCombinedEnv = Join-Path $serverDir "secrets\container_tokens.env"
    if (Test-Path $legacyCombinedEnv) {
        Remove-Item -Path $legacyCombinedEnv -Force -ErrorAction SilentlyContinue
    }
} catch {
    Write-Host "[警告] 写入 secrets\*_tokens.env 失败: $($_.Exception.Message)" -ForegroundColor Yellow
}

# 4. 启动小智与 Anima Brain 双容器
Write-Host "[4/4] 正在启动小智主服务与长期记忆大脑容器..." -ForegroundColor Cyan
if (-not $env:DEEPSEEK_API_KEY -and -not $env:GEMINI_API_KEY) {
    Write-Host "[警告] 未检测到 DEEPSEEK_API_KEY 或 GEMINI_API_KEY 环境变量，请先配置！" -ForegroundColor Yellow
}

$gitShortSha = "dev"
try {
    $shaOut = (& git -C $serverDir rev-parse --short HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $shaOut) {
        $gitShortSha = ($shaOut | Select-Object -First 1).ToString().Trim()
    }
} catch {}
if (-not $env:ANIMA_BUILD_VERSION) { $env:ANIMA_BUILD_VERSION = $gitShortSha }
if (-not $env:XIAOZHI_BUILD_VERSION) { $env:XIAOZHI_BUILD_VERSION = $gitShortSha }

$animaImg = docker images -q xiaozhi-anima-brain:local 2>$null
$needAnimaBuild = (-not $animaImg)
if (-not $needAnimaBuild -and $env:XIAOZHI_SKIP_ANIMA_BUILD -ne "1") {
    $builtRev = ""
    try {
        $revOut = (docker inspect xiaozhi-anima-brain:local --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $revOut) { $builtRev = ($revOut | Select-Object -First 1).ToString().Trim() }
    } catch {}
    $animaDirty = $false
    try {
        $dirtyOut = (& git -C $serverDir status --porcelain -- anima-brain 2>$null)
        if ($LASTEXITCODE -eq 0 -and $dirtyOut) { $animaDirty = $true }
    } catch {}
    if ($animaDirty -or ($env:ANIMA_BUILD_VERSION -ne "dev" -and $builtRev -ne $env:ANIMA_BUILD_VERSION)) {
        $needAnimaBuild = $true
    }
}
if ($needAnimaBuild) {
    docker compose build anima-brain
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n[错误] anima-brain 镜像构建失败，请检查上方报错信息！" -ForegroundColor Red
        exit 1
    }
}

# H-2 非 root (USER app, uid 1000) 平滑升级：若宿主机旧版 root 遗留文件阻塞 uid 1000 读写 /data/anima 或 /obsidian，则一次性归属修正
$animaDataHost = Join-Path $serverDir "anima-data"
if (-not (Test-Path $animaDataHost)) { New-Item -ItemType Directory -Path $animaDataHost -Force | Out-Null }
$obsidianHost = if ($env:OBSIDIAN_EXPORT_DIR) { $env:OBSIDIAN_EXPORT_DIR } else { Join-Path $serverDir "data\obsidian_export" }
if (-not (Test-Path $obsidianHost)) { New-Item -ItemType Directory -Path $obsidianHost -Force | Out-Null }
try {
    $permProbe = "import os, pathlib, sys`ndef ok_dir(p): return os.access(p, os.R_OK | os.W_OK | os.X_OK)`ndef ok_file(p): return (not p.exists()) or (os.access(p, os.R_OK | os.W_OK))`ndata = pathlib.Path('/data/anima')`nobs = pathlib.Path('/obsidian')`nchecks = [ok_dir(data), ok_file(data / 'brain.sqlite3'), ok_file(data / 'studio_token.txt'), (not (data / 'backups').exists()) or ok_dir(data / 'backups'), ok_dir(obs)]`ntry:`n    for dp, _, _ in os.walk(obs, onerror=lambda e: (_ for _ in ()).throw(e)):`n        checks.append(ok_dir(dp))`nexcept Exception:`n    checks.append(False)`nsys.exit(0 if all(checks) else 1)"
    docker run --rm --user 1000:1000 -v "${animaDataHost}:/data/anima" -v "${obsidianHost}:/obsidian" xiaozhi-anima-brain:local python -c $permProbe 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[迁移] 检测到旧版 root 权限文件，正在平滑调整 /data/anima 与 /obsidian 属主为 app (1000:1000)..." -ForegroundColor DarkGray
        docker run --rm --user 0:0 -v "${animaDataHost}:/data/anima" -v "${obsidianHost}:/obsidian" xiaozhi-anima-brain:local chown -R 1000:1000 /data/anima /obsidian 2>$null
    }
} catch {}

docker compose up -d

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

# 5. 若启用了本地 GPU 语音引擎，则通过 /prime 预热全部 7 种情绪参考缓存，再短句预热一次 CUDA 算子 (F-3)
if ($hasLocalGsv) {
    $voicesMeiDir = Join-Path $serverDir "voices\raiden_mei"
    $warmRef = Join-Path $voicesMeiDir "mei_default_ref.wav"
    if (-not (Test-Path $warmRef)) {
        $warmRef = Join-Path $serverDir "data\voices\raiden_mei\mei_default_ref.wav"
        $voicesMeiDir = Split-Path -Parent $warmRef
    }
    if (Test-Path $warmRef) {
        try {
            for ($w = 0; $w -lt 90; $w++) {
                if (Get-NetTCPConnection -LocalPort 19880 -State Listen -ErrorAction SilentlyContinue) { break }
                Start-Sleep -Seconds 1
            }

            $primeItems = @()
            $shortShapeItems = @()
            $emoManifest = Join-Path $voicesMeiDir "mei_emotions.json"
            if (Test-Path $emoManifest) {
                try {
                    $mObj = (Get-Content -Path $emoManifest -Raw -Encoding UTF8) | ConvertFrom-Json
                    if ($mObj -and $mObj.emotions) {
                        foreach ($prop in $mObj.emotions.PSObject.Properties) {
                            $ep = $prop.Value
                            $wavName = Split-Path -Leaf ([string]$ep.ref_audio_path)
                            $localWav = Join-Path $voicesMeiDir $wavName
                            if (Test-Path $localWav) {
                                $pItem = @{
                                    ref_audio_path = $localWav.Replace("\", "/")
                                    prompt_text    = [string]$ep.prompt_text
                                    prompt_lang    = if ($ep.prompt_lang) { [string]$ep.prompt_lang } else { "zh" }
                                }
                                $primeItems += $pItem
                                # v4 T_ref=500 (5.0s @ 48kHz/480): 参考音频短于 5.0s 时 T_min 卷积张量形状独立，需一次极短 sample_steps=4 暖机消除首次切换 cuDNN 编译开销
                                $dur = 0.0
                                if ($ep.PSObject.Properties["duration_sec"]) {
                                    try { $dur = [double]$ep.duration_sec } catch { $dur = 0.0 }
                                }
                                if ($dur -gt 0.0 -and $dur -lt 5.05) {
                                    $shortShapeItems += $pItem
                                }
                            }
                        }
                    }
                } catch {}
            }
            if ($primeItems.Count -eq 0) {
                $primeItems += @{
                    ref_audio_path = $warmRef.Replace("\", "/")
                    prompt_text    = "今夜月色很美，呵...适合与朋友小聚一番。"
                    prompt_lang    = "zh"
                }
            }

            $primeBody = @{ items = $primeItems; skip_if_busy = $false; reason = "startup_7_emotions" } | ConvertTo-Json -Depth 4 -Compress
            $primeResp = Invoke-RestMethod -Uri "http://127.0.0.1:19880/prime" -Method Post -TimeoutSec 60 `
                -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($primeBody))
            if ($primeResp -and $primeResp.ok) {
                Write-Host "[预热] 语音引擎 7 情绪参考特征缓存已就绪 (slots=$($primeResp.cache_slots), $($primeResp.elapsed_ms)ms)" -ForegroundColor DarkGreen
            }

            $warm = @{
                text = "预热测试，银行行长今天很开心。"; text_lang = "zh"; text_split_method = "cut0"; batch_size = 1; media_type = "wav"; sample_steps = 8
                ref_audio_path = $warmRef.Replace("\", "/")
                prompt_text = "今夜月色很美，呵...适合与朋友小聚一番。"; prompt_lang = "zh"
            } | ConvertTo-Json -Compress
            Invoke-WebRequest -Uri "http://127.0.0.1:19880/tts" -Method Post -UseBasicParsing -TimeoutSec 60 `
                -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($warm)) | Out-Null

            foreach ($si in $shortShapeItems) {
                $shortWarm = @{
                    text = "嗯。"; text_lang = "zh"; text_split_method = "cut0"; batch_size = 1; media_type = "wav"; sample_steps = 4
                    ref_audio_path = $si.ref_audio_path
                    prompt_text = $si.prompt_text
                    prompt_lang = $si.prompt_lang
                } | ConvertTo-Json -Compress
                Invoke-WebRequest -Uri "http://127.0.0.1:19880/tts" -Method Post -UseBasicParsing -TimeoutSec 30 `
                    -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($shortWarm)) | Out-Null
            }
            if ($shortShapeItems.Count -gt 0) {
                Write-Host "[预热] 已完成 $($shortShapeItems.Count) 种短参考情绪 cuDNN 形状暖机（首次切换即达缓存速度）" -ForegroundColor DarkGreen
            }
        } catch {
            Write-Host "[提示] 语音引擎预热失败（不影响使用）: $($_.Exception.Message)" -ForegroundColor DarkYellow
        }
    }
}

# G-3: 改用 %UserProfile%\.wslconfig 的 [experimental] autoMemoryReclaim=gradual 持续回收内存，不再一次性执行 drop_caches
try {
    $wslCfgPath = Join-Path $env:USERPROFILE ".wslconfig"
    $hasAutoReclaim = $false
    if (Test-Path $wslCfgPath) {
        $wslCfgText = [System.IO.File]::ReadAllText($wslCfgPath, [System.Text.Encoding]::UTF8)
        $curSection = ""
        foreach ($rawLn in ($wslCfgText -split "\r?\n")) {
            $ln = $rawLn.Trim()
            if (-not $ln -or $ln.StartsWith("#") -or $ln.StartsWith(";")) { continue }
            if ($ln -match '^\[([^\]]+)\]') {
                $curSection = $Matches[1].Trim().ToLowerInvariant()
                continue
            }
            if ($curSection -eq "experimental" -and $ln -match '^autoMemoryReclaim\s*=\s*(gradual|dropcache)\b') {
                $hasAutoReclaim = $true
                break
            }
        }
    }
    if (-not $hasAutoReclaim) {
        Write-Host "[提示] 未在 $wslCfgPath 的 [experimental] 段检测到 autoMemoryReclaim=gradual；建议添加以持续平滑回收 Vmmem 内存（需 WSL >= 2.0）。" -ForegroundColor Yellow
    }
} catch {}

Write-Host "`n[启动成功] 全部服务已就绪！" -ForegroundColor Green
Write-Host "  - 主控制台 (挡位 / L3引擎 / 音响 / 字数 / 亲密度): http://localhost:18080" -ForegroundColor White
Write-Host "  - 记忆大脑控制台:                                  http://localhost:18787" -ForegroundColor White
Write-Host "  - ESP32 OTA 地址:                                  http://$($env:HOST_LAN_IP):18003/xiaozhi/ota/" -ForegroundColor White
Write-Host "---------------------------------------------------------"
