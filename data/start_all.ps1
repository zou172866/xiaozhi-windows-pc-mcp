$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Test-IsExcludedLanIPv4 {
    param([string]$Ip)
    if (-not $Ip) { return $true }
    $addr = $null
    if (-not [System.Net.IPAddress]::TryParse($Ip.Trim(), [ref]$addr)) { return $true }
    if ($addr.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { return $true }
    $b = $addr.GetAddressBytes()
    if ($b[0] -eq 0 -or $b[0] -eq 127) { return $true }
    if ($b[0] -eq 169 -and $b[1] -eq 254) { return $true }
    # 198.18.0.0/15 (Clash / Mihomo / sing-box Fake-IP TUN)
    if ($b[0] -eq 198 -and ($b[1] -eq 18 -or $b[1] -eq 19)) { return $true }
    # 100.64.0.0/10 (Tailscale / CGNAT)
    if ($b[0] -eq 100 -and $b[1] -ge 64 -and $b[1] -le 127) { return $true }
    if ($b[0] -ge 224) { return $true }
    return $false
}

function Test-IsVirtualAdapterName {
    param(
        [string]$Alias = "",
        [string]$Description = ""
    )
    $combined = "$Alias $Description"
    return ($combined -match '(?i)(WSL|vEthernet|Docker|Loopback|Hyper-V|VirtualBox|VMware|Clash|Mihomo|\bMeta\b|FlClash|sing-?box|sing-?tun|Wintun|WireGuard|Tailscale|ZeroTier|OpenVPN|TAP-Windows|TAP-Win32|\bTUN\d*\b|VPN|WARP|Cloudflare|Radmin|Hamachi|EasyTier|Netch|Nekoray|v2ray|Bluetooth|Npcap|Kernel Debug)')
}

function Get-PrivateLanRank {
    param([string]$Ip)
    $addr = $null
    if (-not [System.Net.IPAddress]::TryParse($Ip.Trim(), [ref]$addr)) { return 9 }
    $b = $addr.GetAddressBytes()
    if ($b[0] -eq 192 -and $b[1] -eq 168) { return 0 }
    if ($b[0] -eq 10) { return 1 }
    if ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) { return 2 }
    return 3
}

function Select-BestLanIPv4 {
    param([object[]]$Candidates = $null)
    $rows = @()
    if ($null -eq $Candidates) {
        try {
            $adapterByIdx = @{}
            foreach ($ad in (Get-NetAdapter -ErrorAction SilentlyContinue)) {
                $adapterByIdx[[int]$ad.ifIndex] = $ad
            }
            $metricByIdx = @{}
            foreach ($iface in (Get-NetIPInterface -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
                if ($null -ne $iface.InterfaceMetric) {
                    $metricByIdx[[int]$iface.ifIndex] = [int]$iface.InterfaceMetric
                }
            }
            foreach ($ipObj in (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
                $ifIdx = [int]$ipObj.InterfaceIndex
                $ad = $adapterByIdx[$ifIdx]
                $metricVal = if ($metricByIdx.ContainsKey($ifIdx)) {
                    $metricByIdx[$ifIdx]
                } elseif ($ipObj.PSObject.Properties["InterfaceMetric"] -and $null -ne $ipObj.InterfaceMetric) {
                    [int]$ipObj.InterfaceMetric
                } else {
                    9999
                }
                $rows += [pscustomobject]@{
                    IPAddress            = [string]$ipObj.IPAddress
                    InterfaceAlias       = [string]$ipObj.InterfaceAlias
                    InterfaceDescription = if ($ad) { [string]$ad.InterfaceDescription } else { "" }
                    Status               = if ($ad) { [string]$ad.Status } else { "Up" }
                    Virtual              = if ($ad -and $null -ne $ad.Virtual) { [bool]$ad.Virtual } else { $false }
                    InterfaceMetric      = $metricVal
                }
            }
        } catch {}
    } else {
        $rows = @($Candidates)
    }

    $valid = @()
    foreach ($r in $rows) {
        $ip = [string]($r.IPAddress)
        $alias = [string]($r.InterfaceAlias)
        $desc = if ($r.PSObject.Properties["InterfaceDescription"]) { [string]$r.InterfaceDescription } else { "" }
        $status = if ($r.PSObject.Properties["Status"] -and $r.Status) { [string]$r.Status } else { "Up" }
        $isVirt = if ($r.PSObject.Properties["Virtual"]) { [bool]$r.Virtual } else { $false }
        $metric = if ($r.PSObject.Properties["InterfaceMetric"] -and $null -ne $r.InterfaceMetric) { [int]$r.InterfaceMetric } else { 9999 }

        if ($status -ne "Up") { continue }
        if (Test-IsExcludedLanIPv4 -Ip $ip) { continue }
        if (Test-IsVirtualAdapterName -Alias $alias -Description $desc) { continue }

        $valid += [pscustomobject]@{
            IPAddress            = $ip.Trim()
            InterfaceAlias       = $alias
            InterfaceDescription = $desc
            LanRank              = (Get-PrivateLanRank -Ip $ip)
            VirtRank             = if ($isVirt) { 1 } else { 0 }
            InterfaceMetric      = $metric
        }
    }

    return @($valid | Sort-Object -Property LanRank, VirtRank, InterfaceMetric, IPAddress)
}

function Get-ComposeXiaozhiImageInfo {
    param([string]$ComposePath)
    $primary = "ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest@sha256:cececd7752e00e044bf9f099751a140834053573bbe6dd55ecbcedcb20c19c25"
    if ($ComposePath -and (Test-Path $ComposePath)) {
        try {
            $raw = [System.IO.File]::ReadAllText($ComposePath, [System.Text.Encoding]::UTF8)
            $m = [regex]::Match($raw, '(?m)^\s*image:\s*(?:\$\{[A-Za-z_][A-Za-z0-9_]*:-)?([^\s}]+xiaozhi-esp32-server[^\s}]+)\}?\s*$')
            if ($m.Success -and $m.Groups[1].Value) {
                $primary = $m.Groups[1].Value.Trim()
            }
        } catch {}
    }
    $fallback = ""
    if ($primary -match '^ghcr\.nju\.edu\.cn/') {
        $fallback = $primary -replace '^ghcr\.nju\.edu\.cn/', 'ghcr.io/'
    }
    return [pscustomobject]@{
        Primary  = $primary
        Fallback = $fallback
    }
}

function Get-AnimaProviderKind {
    param([string]$Url)
    $s = if ($null -ne $Url) { $Url.Trim() } else { "" }
    if (-not $s) { return "deepseek" }
    $hostName = ""
    try {
        $uri = [System.Uri]$s
        if ($uri -and $uri.IsAbsoluteUri -and $uri.Host) {
            $hostName = $uri.Host.Trim().ToLowerInvariant()
        }
    } catch {}
    if ($hostName -eq "generativelanguage.googleapis.com" -or $hostName.EndsWith(".googleapis.com")) {
        return "gemini"
    }
    if ($hostName -eq "api.deepseek.com" -or $hostName.EndsWith(".deepseek.com")) {
        return "deepseek"
    }
    return "other"
}

function Resolve-AnimaEnvFallbacks {
    param(
        [hashtable]$EnvMap,
        [hashtable]$ExplicitEmpty = @{}
    )
    $out = @{}
    foreach ($k in $EnvMap.Keys) { $out[$k] = [string]$EnvMap[$k] }

    $dsKey = if ($out.ContainsKey("DEEPSEEK_API_KEY")) { $out["DEEPSEEK_API_KEY"].Trim() } else { "" }
    $gemKey = if ($out.ContainsKey("GEMINI_API_KEY")) { $out["GEMINI_API_KEY"].Trim() } else { "" }
    $llmUrl = if ($out.ContainsKey("ANIMA_LLM_BASE_URL")) { $out["ANIMA_LLM_BASE_URL"].Trim() } else { "" }
    $llmKey = if ($out.ContainsKey("ANIMA_LLM_API_KEY")) { $out["ANIMA_LLM_API_KEY"].Trim() } else { "" }
    $sumUrl = if ($out.ContainsKey("ANIMA_SUMMARY_BASE_URL")) { $out["ANIMA_SUMMARY_BASE_URL"].Trim() } else { "" }
    $sumKey = if ($out.ContainsKey("ANIMA_SUMMARY_API_KEY")) { $out["ANIMA_SUMMARY_API_KEY"].Trim() } else { "" }
    $embUrl = if ($out.ContainsKey("ANIMA_EMBEDDING_BASE_URL")) { $out["ANIMA_EMBEDDING_BASE_URL"].Trim() } else { "" }
    $embKey = if ($out.ContainsKey("ANIMA_EMBEDDING_API_KEY")) { $out["ANIMA_EMBEDDING_API_KEY"].Trim() } else { "" }

    # If user never configured ANIMA_LLM_BASE_URL and only has GEMINI_API_KEY (no DEEPSEEK_API_KEY),
    # route Anima LLM cleanly to Gemini instead of sending GEMINI_API_KEY to DeepSeek.
    if (-not $llmUrl -and -not $llmKey -and -not $dsKey -and $gemKey) {
        $llmUrl = "https://generativelanguage.googleapis.com/v1beta/openai"
        $out["ANIMA_LLM_BASE_URL"] = $llmUrl
        if (-not ($out.ContainsKey("ANIMA_LLM_MODEL") -and $out["ANIMA_LLM_MODEL"].Trim())) {
            $out["ANIMA_LLM_MODEL"] = "gemini-3.5-flash-lite"
        }
    }

    $effLlmUrl = if ($llmUrl) { $llmUrl } else { "https://api.deepseek.com/v1" }
    $llmKind = Get-AnimaProviderKind -Url $effLlmUrl
    if (-not $llmKey -and -not $ExplicitEmpty.ContainsKey("ANIMA_LLM_API_KEY")) {
        if ($llmKind -eq "gemini" -and $gemKey) {
            $out["ANIMA_LLM_API_KEY"] = $gemKey
        } elseif ($llmKind -eq "deepseek" -and $dsKey) {
            $out["ANIMA_LLM_API_KEY"] = $dsKey
        }
    }

    $effSumUrl = if ($sumUrl) { $sumUrl } else { $effLlmUrl }
    $sumKind = Get-AnimaProviderKind -Url $effSumUrl
    if (-not $sumKey -and -not $ExplicitEmpty.ContainsKey("ANIMA_SUMMARY_API_KEY")) {
        if ($sumKind -eq "gemini" -and $gemKey) {
            $out["ANIMA_SUMMARY_API_KEY"] = $gemKey
        } elseif ($sumKind -eq "deepseek" -and $dsKey) {
            $out["ANIMA_SUMMARY_API_KEY"] = $dsKey
        }
    }

    if ($embUrl -and -not $embKey -and -not $ExplicitEmpty.ContainsKey("ANIMA_EMBEDDING_API_KEY")) {
        $embKind = Get-AnimaProviderKind -Url $embUrl
        if ($embKind -eq "gemini" -and $gemKey) {
            $out["ANIMA_EMBEDDING_API_KEY"] = $gemKey
        } elseif ($embKind -eq "deepseek" -and $dsKey) {
            $out["ANIMA_EMBEDDING_API_KEY"] = $dsKey
        }
    }

    return $out
}

function Normalize-DockerMirrorPrefix {
    param([string]$Prefix)
    if (-not $Prefix) { return "" }
    $s = $Prefix.Trim()
    if (-not $s) { return "" }
    if (-not $s.EndsWith("/")) { $s = "$s/" }
    return $s
}

function Find-DockerDesktopExe {
    $candidates = [System.Collections.Generic.List[string]]::new()
    foreach ($regPath in @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop"
    )) {
        try {
            if (Test-Path $regPath) {
                $item = Get-ItemProperty -Path $regPath -ErrorAction SilentlyContinue
                if ($item -and $item.InstallLocation) {
                    $candidates.Add((Join-Path $item.InstallLocation "Docker Desktop.exe"))
                }
            }
        } catch {}
    }
    foreach ($envBase in @($env:ProgramFiles, $env:ProgramW6432)) {
        if ($envBase) { $candidates.Add((Join-Path $envBase "Docker\Docker\Docker Desktop.exe")) }
    }
    if ($env:LOCALAPPDATA) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe"))
    }
    try {
        $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
        if ($dockerCmd -and $dockerCmd.Source) {
            # Prefer outermost install dir above resources\bin\docker.exe (skip internal resources\Docker desktop.exe)
            $ancestors = [System.Collections.Generic.List[string]]::new()
            $cur = Split-Path -Parent $dockerCmd.Source
            for ($depth = 0; $depth -lt 5 -and $cur; $depth++) {
                if ((Split-Path -Leaf $cur) -ine "resources" -and (Split-Path -Leaf $cur) -ine "frontend") {
                    $ancestors.Add($cur)
                }
                $parent = Split-Path -Parent $cur
                if ($parent -eq $cur) { break }
                $cur = $parent
            }
            for ($idx = $ancestors.Count - 1; $idx -ge 0; $idx--) {
                $candidates.Add((Join-Path $ancestors[$idx] "Docker Desktop.exe"))
            }
        }
    } catch {}
    foreach ($dc in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path $dc) { return $dc }
    }
    return ""
}

function Get-ActiveAsrModule {
    param([string]$RuntimeConfigPath)
    if (-not $RuntimeConfigPath -or -not (Test-Path $RuntimeConfigPath)) { return "SherpaASR" }
    try {
        $raw = Get-Content -Path $RuntimeConfigPath -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($raw) {
            $m = [regex]::Match($raw, '(?m)^[ \t]+ASR:[ \t]+["'']?([A-Za-z0-9_]+)["'']?\b')
            if ($m.Success -and $m.Groups[1].Value) {
                return $m.Groups[1].Value.Trim()
            }
        }
    } catch {}
    return "SherpaASR"
}

function Test-PortSpecMatches {
    param([object]$LocalPort, [int]$TargetPort)
    foreach ($entry in @($LocalPort)) {
        $rawStr = [string]$entry
        if (-not $rawStr) { continue }
        foreach ($tok in ($rawStr -split ',')) {
            $s = $tok.Trim()
            if (-not $s) { continue }
            if ($s -eq [string]$TargetPort) { return $true }
            if ($s -match '^(\d+)-(\d+)$') {
                $lo = [int]$Matches[1]; $hi = [int]$Matches[2]
                if ($TargetPort -ge $lo -and $TargetPort -le $hi) { return $true }
            }
        }
    }
    return $false
}

function Evaluate-FirewallRulesForPorts {
    param(
        [int[]]$Ports = @(18000, 18003),
        [int]$ActiveProfileMask = 2,
        [bool]$FirewallActive = $true,
        [object[]]$Rules = @()
    )
    $res = [pscustomobject]@{
        Checked           = $true
        FirewallActive    = $FirewallActive
        ActiveProfileMask = $ActiveProfileMask
        HasPublicProfile  = (($ActiveProfileMask -band 4) -ne 0)
        AllowedPorts      = @()
        MissingPorts      = @()
    }
    if (-not $FirewallActive) {
        $res.AllowedPorts = @($Ports)
        return $res
    }
    $matchingRules = @()
    foreach ($r in @($Rules)) {
        if (-not $r) { continue }
        if (-not [bool]$r.Enabled) { continue }
        if ([int]$r.Direction -ne 1) { continue }
        if ([int]$r.Action -ne 1) { continue }
        $proto = [int]$r.Protocol
        if ($proto -ne 6 -and $proto -ne 256) { continue }
        $appName = if ($r.PSObject.Properties["ApplicationName"] -and $null -ne $r.ApplicationName) { [string]$r.ApplicationName } else { "" }
        if ($appName.Trim()) { continue }
        $remoteAddr = if ($r.PSObject.Properties["RemoteAddresses"] -and $null -ne $r.RemoteAddresses) { [string]$r.RemoteAddresses } else { "*" }
        if ($remoteAddr.Trim() -match '^(127\.0\.0\.1|::1)(/\d+)?$') { continue }
        $ruleProf = [int]$r.Profiles
        if ($ActiveProfileMask -ne 0 -and (($ruleProf -band $ActiveProfileMask) -ne $ActiveProfileMask)) { continue }
        $matchingRules += $r
    }
    foreach ($port in $Ports) {
        $matched = $false
        foreach ($r in $matchingRules) {
            $lp = if ($r.PSObject.Properties["LocalPorts"] -and $null -ne $r.LocalPorts) { ([string]$r.LocalPorts).Trim() } else { "" }
            if ($lp -eq "*" -or $lp -ieq "Any" -or (Test-PortSpecMatches -LocalPort $lp -TargetPort $port)) {
                $matched = $true
                break
            }
        }
        if ($matched) { $res.AllowedPorts += $port } else { $res.MissingPorts += $port }
    }
    return $res
}

function Test-XiaozhiFirewallPorts {
    param([int[]]$Ports = @(18000, 18003))
    try {
        # HNetCfg.FwPolicy2 COM works without Administrator elevation (unlike Get-NetFirewallRule CIM)
        $fw = New-Object -ComObject HNetCfg.FwPolicy2
        # Scope profile mask to the primary physical LAN adapter so unrelated WSL/Hyper-V Public NICs don't cause false positives
        $curMask = 0
        try {
            $bestLan = @(Select-BestLanIPv4)
            if ($bestLan.Count -gt 0) {
                $lanAlias = [string]$bestLan[0].InterfaceAlias
                $profObj = Get-NetConnectionProfile -InterfaceAlias $lanAlias -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($profObj) {
                    switch ([string]$profObj.NetworkCategory) {
                        "DomainAuthenticated" { $curMask = 1 }
                        "Private"             { $curMask = 2 }
                        "Public"              { $curMask = 4 }
                    }
                }
            }
        } catch {}
        if ($curMask -le 0) { $curMask = [int]$fw.CurrentProfileTypes }
        if ($curMask -le 0) { $curMask = 2 }
        $enabledActiveMask = 0
        foreach ($bit in @(1, 2, 4)) {
            if ((($curMask -band $bit) -ne 0) -and [bool]$fw.FirewallEnabled($bit)) {
                $enabledActiveMask = $enabledActiveMask -bor $bit
            }
        }
        if ($enabledActiveMask -eq 0) {
            return [pscustomobject]@{
                Checked           = $true
                FirewallActive    = $false
                ActiveProfileMask = $curMask
                HasPublicProfile  = (($curMask -band 4) -ne 0)
                AllowedPorts      = @($Ports)
                MissingPorts      = @()
            }
        }
        $ruleSnap = @()
        foreach ($r in $fw.Rules) {
            if (-not $r.Enabled -or $r.Direction -ne 1 -or $r.Action -ne 1) { continue }
            if ($r.Protocol -ne 6 -and $r.Protocol -ne 256) { continue }
            if (-not [string]::IsNullOrWhiteSpace($r.ApplicationName)) { continue }
            $ruleSnap += [pscustomobject]@{
                Enabled         = [bool]$r.Enabled
                Direction       = [int]$r.Direction
                Action          = [int]$r.Action
                Protocol        = [int]$r.Protocol
                LocalPorts      = [string]$r.LocalPorts
                Profiles        = [int]$r.Profiles
                ApplicationName = [string]$r.ApplicationName
                RemoteAddresses = [string]$r.RemoteAddresses
            }
        }
        return (Evaluate-FirewallRulesForPorts -Ports $Ports -ActiveProfileMask $enabledActiveMask -FirewallActive $true -Rules $ruleSnap)
    } catch {
        return [pscustomobject]@{
            Checked           = $false
            FirewallActive    = $false
            ActiveProfileMask = 0
            HasPublicProfile  = $false
            AllowedPorts      = @()
            MissingPorts      = @()
        }
    }
}

if ($env:XIAOZHI_START_ALL_LIB_ONLY -eq "1") {
    return
}

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

foreach ($envName in @(
    "DEEPSEEK_API_KEY", "GEMINI_API_KEY",
    "ANIMA_LLM_API_KEY", "ANIMA_SUMMARY_API_KEY", "ANIMA_EMBEDDING_API_KEY",
    "SILICONFLOW_API_KEY", "OPENAI_AUDIO_API_KEY", "DASHSCOPE_API_KEY",
    "DOUBAO_APPID", "DOUBAO_ACCESS_TOKEN",
    "DOUBAO_ASR_APPID", "DOUBAO_ASR_ACCESS_TOKEN",
    "DOUBAO_TTS_APPID", "DOUBAO_TTS_ACCESS_TOKEN",
    "FISHSPEECH_API_KEY", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID"
)) {
    if ($explicitEmptyEnvKeys.ContainsKey($envName)) { continue }
    $val = [Environment]::GetEnvironmentVariable($envName, "User")
    if (-not $val) { $val = [Environment]::GetEnvironmentVariable($envName, "Machine") }
    if ($val) {
        Set-Item -Path "Env:$envName" -Value ($val.TrimStart([char]0xfeff).Trim())
    }
}

# 按 URL 自动补齐 Anima Brain 匹配密钥（进程内生效，绝不落盘，且绝不将 DeepSeek Key 误发给 Gemini）
$animaResolveIn = @{
    "DEEPSEEK_API_KEY"         = [string]$env:DEEPSEEK_API_KEY
    "GEMINI_API_KEY"           = [string]$env:GEMINI_API_KEY
    "ANIMA_LLM_BASE_URL"       = [string]$env:ANIMA_LLM_BASE_URL
    "ANIMA_LLM_MODEL"          = [string]$env:ANIMA_LLM_MODEL
    "ANIMA_LLM_API_KEY"        = [string]$env:ANIMA_LLM_API_KEY
    "ANIMA_SUMMARY_BASE_URL"   = [string]$env:ANIMA_SUMMARY_BASE_URL
    "ANIMA_SUMMARY_API_KEY"    = [string]$env:ANIMA_SUMMARY_API_KEY
    "ANIMA_EMBEDDING_BASE_URL" = [string]$env:ANIMA_EMBEDDING_BASE_URL
    "ANIMA_EMBEDDING_API_KEY"  = [string]$env:ANIMA_EMBEDDING_API_KEY
}
$animaResolved = Resolve-AnimaEnvFallbacks -EnvMap $animaResolveIn -ExplicitEmpty $explicitEmptyEnvKeys
foreach ($rk in @("ANIMA_LLM_BASE_URL", "ANIMA_LLM_MODEL", "ANIMA_LLM_API_KEY", "ANIMA_SUMMARY_API_KEY", "ANIMA_EMBEDDING_API_KEY")) {
    if ($animaResolved[$rk] -and $animaResolved[$rk] -ne $animaResolveIn[$rk]) {
        Set-Item -Path "Env:$rk" -Value $animaResolved[$rk]
    }
}
$effAnimaKind = Get-AnimaProviderKind -Url ([string]$env:ANIMA_LLM_BASE_URL)
if ($effAnimaKind -eq "deepseek" -and $env:ANIMA_LLM_API_KEY -and ($env:ANIMA_LLM_API_KEY -match '^AIza')) {
    Write-Host "[警告] 当前 ANIMA_LLM_BASE_URL 指向 DeepSeek，但环境变量 ANIMA_LLM_API_KEY 似乎是旧的 Gemini Key（AIza...），请运行「一键初始化配置.bat」清理旧环境变量！" -ForegroundColor Yellow
} elseif ($effAnimaKind -eq "gemini" -and $env:ANIMA_LLM_API_KEY -and ($env:ANIMA_LLM_API_KEY -match '^sk-')) {
    Write-Host "[警告] 当前 ANIMA_LLM_BASE_URL 指向 Google Gemini，但环境变量 ANIMA_LLM_API_KEY 似乎是旧的 DeepSeek Key（sk-...），请运行「一键初始化配置.bat」清理旧环境变量！" -ForegroundColor Yellow
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
            if ($curEnvText -match '(?m)^[ \t]*ESP32_AUTH_KEY[ \t]*=[ \t]*(\r?)$') {
                $updatedEnvText = [regex]::Replace($curEnvText, '(?m)^[ \t]*ESP32_AUTH_KEY[ \t]*=[ \t]*(\r?)$', "ESP32_AUTH_KEY=$genKey`$1", 1)
                [System.IO.File]::WriteAllText($dotEnvPath, $updatedEnvText, [System.Text.UTF8Encoding]::new($false))
            } elseif ($curEnvText -notmatch '(?m)^\s*ESP32_AUTH_KEY\s*=') {
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
    Write-Host "[提示] .env 中尚未配置 ESP32_ALLOWED_DEVICES；服务启动后可直接在 http://localhost:18080 控制台「一键放行」新接入的 ESP32。" -ForegroundColor Yellow
}
if ($env:OBSIDIAN_EXPORT_DIR -and -not (Test-Path $env:OBSIDIAN_EXPORT_DIR)) {
    try { New-Item -ItemType Directory -Path $env:OBSIDIAN_EXPORT_DIR -Force | Out-Null } catch {}
}
$lanCandidates = @(Select-BestLanIPv4)
if ($env:HOST_LAN_IP -and (Test-IsExcludedLanIPv4 -Ip $env:HOST_LAN_IP)) {
    $suggestIp = if ($lanCandidates.Count -gt 0) { $lanCandidates[0].IPAddress } else { "" }
    $sugHint = if ($suggestIp) { "（建议改为真实局域网 IP: $suggestIp）" } else { "" }
    Write-Host "[警告] 当前 HOST_LAN_IP=$($env:HOST_LAN_IP) 属于虚拟/代理/回环网段，ESP32 可能无法连入$sugHint" -ForegroundColor Yellow
}
if (-not $env:HOST_LAN_IP) {
    if ($lanCandidates.Count -gt 0) {
        $env:HOST_LAN_IP = $lanCandidates[0].IPAddress
    }
}
if (-not $env:HOST_LAN_IP) { $env:HOST_LAN_IP = "127.0.0.1" }
Write-Host "[环境] 局域网 IP: $($env:HOST_LAN_IP) | 数据目录: $($env:HOST_DATA_DIR)" -ForegroundColor DarkGray

# 1. 确保 Docker Desktop 已安装、启动并就绪（缺失或启动失败立即退出，严禁误报就绪）
$dockerCli = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCli) {
    Write-Host "`n[错误] 未检测到 docker 命令！请先安装 Docker Desktop（启用 WSL2 后端）后再启动：" -ForegroundColor Red
    Write-Host "       https://www.docker.com/products/docker-desktop/" -ForegroundColor Yellow
    exit 1
}

$ver = (docker info --format "{{.ServerVersion}}" 2>$null)
if ($LASTEXITCODE -ne 0 -or -not ($ver -and $ver.ToString().Trim())) {
    $dockerExe = Find-DockerDesktopExe
    $dockerSvc = Get-Service -Name "com.docker.service" -ErrorAction SilentlyContinue
    if (-not $dockerExe -and -not $dockerSvc) {
        Write-Host "`n[错误] Docker 守护进程未运行，且本机未找到 Docker Desktop 安装路径！" -ForegroundColor Red
        Write-Host "       请先安装并启动 Docker Desktop: https://www.docker.com/products/docker-desktop/" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "[1/4] 检测到 Docker 未运行，正在自动启动 Docker Desktop..." -ForegroundColor Yellow
    if ($dockerSvc) { Start-Service com.docker.service -ErrorAction SilentlyContinue }
    if ($dockerExe) {
        Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "`"$dockerExe`"" } | Out-Null
    }
    $ver = ""
    for ($i = 1; $i -le 45; $i++) {
        Start-Sleep -Seconds 2
        $probeVer = (docker info --format "{{.ServerVersion}}" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $probeVer -and $probeVer.ToString().Trim()) {
            $ver = ($probeVer | Select-Object -First 1).ToString().Trim()
            break
        }
        if (($i % 5) -eq 0) {
            Write-Host "  仍在等待 Docker 引擎就绪 ($($i * 2)s / 90s)..." -ForegroundColor DarkGray
        }
    }
    if (-not $ver) {
        Write-Host "`n[错误] 等待 90 秒后 Docker 引擎仍未就绪！" -ForegroundColor Red
        Write-Host "       请检查 Docker Desktop 托盘图标状态（确认 WSL2 已启用、虚拟化已开启、无弹窗阻塞）后重试。" -ForegroundColor Yellow
        exit 1
    }
} else {
    $ver = ($ver | Select-Object -First 1).ToString().Trim()
}

docker compose version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[错误] 当前 Docker 未启用 Compose v2（docker compose 不可用），请升级至新版 Docker Desktop！" -ForegroundColor Red
    exit 1
}
Write-Host "[1/4] Docker 引擎已就绪 (v$ver)" -ForegroundColor Green

# 2. 启动或重启本机三层电脑操控 MCP 服务 (端口 18900，仅绑定 127.0.0.1 + 自动 Bearer Token + 看门狗守护)
$mcpPy = Join-Path $serverDir "pc-mcp\.venv\Scripts\python.exe"
$mcpPyReady = $false
if (Test-Path $mcpPy) {
    & $mcpPy -c "import uvicorn, starlette, httpx" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $mcpPyReady = $true
    } else {
        Write-Host "[警告] 检测到 pc-mcp\.venv 已损坏或来自其他电脑路径，将跳过启动 18900 电脑操控服务；请双击「一键初始化配置.bat」重建虚拟环境！" -ForegroundColor Red
    }
} else {
    Write-Host "[警告] 未找到 pc-mcp\.venv 虚拟环境，强烈建议先双击「一键初始化配置.bat」安装宿主机依赖！" -ForegroundColor Yellow
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd -and $pyCmd.Source -and ($pyCmd.Source -notmatch '(?i)\\WindowsApps\\')) {
        & $pyCmd.Source -c "import uvicorn, starlette, httpx" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $mcpPy = $pyCmd.Source
            $mcpPyReady = $true
        }
    }
}
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
    if ($mcpPyReady -and (Test-Path $mcpWatchdogScript)) {
        try { & $mcpPy -X utf8 $mcpWatchdogScript --cleanup-l3 2>$null | Out-Null } catch {}
    }
    foreach ($omp in $oldMcpPidSet.Keys) {
        Stop-Process -Id $omp -Force -ErrorAction SilentlyContinue
    }
    for ($w = 0; $w -lt 12; $w++) {
        if (-not (Get-NetTCPConnection -LocalPort 18900 -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
} elseif ($mcpPyReady) {
    Write-Host "[2/4] 正在后台启动三层电脑操控 MCP + 电脑音响直通服务 (127.0.0.1:18900)..." -ForegroundColor Cyan
}

if (-not $mcpPyReady) {
    Write-Host "[2/4] 已跳过宿主机 18900 MCP 启动（缺少可用的 Python 虚拟环境，仅影响电脑操控与电脑音响直通，ESP32 语音对话可继续运行）。" -ForegroundColor Yellow
} else {
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
    $activeTtsName = "EdgeTTS"
    $runtimeCfgFile = Join-Path $serverDir "config\runtime\.config.yaml"
    if (Test-Path $runtimeCfgFile) {
        $rawRtCfg = Get-Content -Path $runtimeCfgFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($rawRtCfg) {
            $mTts = [regex]::Match($rawRtCfg, "(?m)^[ \t]+TTS:[ \t]+([A-Za-z0-9_]+)\b")
            if ($mTts.Success -and $mTts.Groups[1].Value -ne "GPT_SOVITS_V2") {
                $activeTtsName = $mTts.Groups[1].Value
            }
        }
    }
    Write-Host "[3/4] 未启用本地 GPU GPT-SoVITS（免显存模式，当前使用 $activeTtsName）" -ForegroundColor DarkGreen
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
if ($env:DOCKER_MIRROR_PREFIX) {
    $env:DOCKER_MIRROR_PREFIX = Normalize-DockerMirrorPrefix -Prefix $env:DOCKER_MIRROR_PREFIX
}
if ($needAnimaBuild) {
    Write-Host "  正在构建 xiaozhi-anima-brain:local 镜像（首次构建需拉取基础镜像与依赖，请耐心等待）..." -ForegroundColor DarkCyan
    docker compose build anima-brain
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n[错误] anima-brain 镜像构建失败，请检查上方报错信息！" -ForegroundColor Red
        Write-Host "       国内网络若卡在拉取镜像或 npm/pip，可在 .env 中配置以下加速项后重试：" -ForegroundColor Yellow
        Write-Host "         NPM_REGISTRY=https://registry.npmmirror.com" -ForegroundColor DarkGray
        Write-Host "         PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" -ForegroundColor DarkGray
        Write-Host "         DOCKER_MIRROR_PREFIX=docker.m.daocloud.io/library/" -ForegroundColor DarkGray
        exit 1
    }
}

# 主容器上游镜像预拉取与 ghcr.nju.edu.cn -> ghcr.io 同 digest 自动回退
if (-not $env:XIAOZHI_SERVER_IMAGE) {
    $imgInfo = Get-ComposeXiaozhiImageInfo -ComposePath (Join-Path $serverDir "docker-compose.yml")
    docker image inspect $imgInfo.Primary 2>$null | Out-Null
    $hasPrimary = ($LASTEXITCODE -eq 0)
    $hasFallback = $false
    if (-not $hasPrimary -and $imgInfo.Fallback) {
        docker image inspect $imgInfo.Fallback 2>$null | Out-Null
        $hasFallback = ($LASTEXITCODE -eq 0)
    }
    if ($hasFallback) {
        $env:XIAOZHI_SERVER_IMAGE = $imgInfo.Fallback
        Write-Host "  [镜像] 已命中本地缓存的官方同摘要镜像: $($imgInfo.Fallback)" -ForegroundColor DarkGray
    } elseif (-not $hasPrimary) {
        Write-Host "  正在拉取小智主服务镜像 (国内南大镜像源)..." -ForegroundColor DarkCyan
        docker pull $imgInfo.Primary
        if ($LASTEXITCODE -ne 0 -and $imgInfo.Fallback) {
            Write-Host "  [回退] 国内镜像源拉取失败，正在自动切换官方 ghcr.io 同摘要镜像..." -ForegroundColor Yellow
            docker pull $imgInfo.Fallback
            if ($LASTEXITCODE -eq 0) {
                $env:XIAOZHI_SERVER_IMAGE = $imgInfo.Fallback
            } else {
                Write-Host "`n[错误] 无法拉取小智主服务镜像（ghcr.nju.edu.cn 与 ghcr.io 均失败），请检查网络连接或 Docker 代理设置！" -ForegroundColor Red
                exit 1
            }
        } elseif ($LASTEXITCODE -ne 0) {
            Write-Host "`n[错误] 无法拉取小智主服务镜像，请检查网络连接或 Docker 代理设置！" -ForegroundColor Red
            exit 1
        }
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

$activeAsrMod = Get-ActiveAsrModule -RuntimeConfigPath (Join-Path $serverDir "config\runtime\.config.yaml")
$sherpaModelFile = Join-Path $serverDir "models\sherpa-onnx-sense-voice\model.int8.onnx"
$isFirstBootModelDownload = ($activeAsrMod -eq "SherpaASR") -and (-not (Test-Path $sherpaModelFile))
$maxReadyWaitSec = if ($isFirstBootModelDownload) { 180 } else { 45 }
if ($isFirstBootModelDownload) {
    Write-Host "检测到首次启动且本地尚未缓存 SherpaASR 模型 (~230MB)，容器正在自动轻量直连下载，最长等待 ${maxReadyWaitSec} 秒..." -ForegroundColor Yellow
} else {
    Write-Host "正在等待语音服务加载就绪 (最长等待 ${maxReadyWaitSec} 秒)..." -ForegroundColor DarkGray
}

$serverReady = $false
$containerFatalExit = $false
for ($j = 1; $j -le $maxReadyWaitSec; $j++) {
    try {
        $st = Invoke-RestMethod -Uri "http://localhost:18080/api/state" -TimeoutSec 2 -ErrorAction Stop
        if ($st -and $st.device -and $st.device.server_ready) {
            $serverReady = $true
            break
        }
    } catch {}
    if (($j % 10) -eq 0) {
        $cState = (docker inspect -f "{{.State.Status}}|{{.State.ExitCode}}" xiaozhi-esp32-server 2>$null)
        if ($LASTEXITCODE -eq 0 -and $cState) {
            $cLine = ($cState | Select-Object -First 1).ToString().Trim()
            if ($cLine -match '^(exited|dead|restarting)\|(\d+)$' -and [int]$Matches[2] -ne 0) {
                Write-Host "`n[错误] 小智主容器异常退出 (state=$cLine)，以下为最近 30 行容器日志：" -ForegroundColor Red
                docker logs --tail 30 xiaozhi-esp32-server
                $containerFatalExit = $true
                break
            }
        }
        Write-Host "  仍在等待语音服务初始化 (${j}s / ${maxReadyWaitSec}s)..." -ForegroundColor DarkGray
    }
    Start-Sleep -Seconds 1
}
if ($containerFatalExit) {
    exit 1
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

# 非阻塞检查 Windows 防火墙 TCP 18000 / 18003 入站放行状态
try {
    $fwCheck = Test-XiaozhiFirewallPorts -Ports @(18000, 18003)
    if ($fwCheck.Checked -and $fwCheck.FirewallActive -and $fwCheck.MissingPorts.Count -gt 0) {
        $missPortsStr = ($fwCheck.MissingPorts -join ", ")
        Write-Host "[提示] Windows 防火墙未检测到当前网络配置下的 TCP $missPortsStr 入站放行规则，若局域网 ESP32 无法连入，请运行「一键初始化配置.bat」或以管理员 PowerShell 执行：" -ForegroundColor Yellow
        Write-Host "       New-NetFirewallRule -DisplayName 'XiaoZhi ESP32 (TCP 18000/18003)' -Direction Inbound -Profile Private -Protocol TCP -LocalPort 18000,18003 -Action Allow" -ForegroundColor DarkGray
        if ($fwCheck.HasPublicProfile) {
            Write-Host "       [注意] 当前网络配置包含「公用网络 (Public)」，建议在 Windows 网络设置中将家庭 Wi-Fi / 以太网设为「专用网络 (Private)」。" -ForegroundColor DarkYellow
        }
    }
} catch {}

if ($serverReady) {
    Write-Host "`n[启动成功] 全部服务已就绪！" -ForegroundColor Green
} else {
    Write-Host "`n[提示] 容器已启动，但语音识别模型或核心服务仍在后台加载中（未在 ${maxReadyWaitSec} 秒内报告 server_ready）。" -ForegroundColor Yellow
    Write-Host "       请打开 http://localhost:18080 观察顶部状态灯，或双击「查看实时日志.bat」查看下载/启动进度。" -ForegroundColor DarkYellow
}
Write-Host "  - 主控制台 (白名单 / 挡位 / L3引擎 / 音响 / 语音): http://localhost:18080" -ForegroundColor White
Write-Host "  - 记忆大脑控制台:                                  http://localhost:18787" -ForegroundColor White
Write-Host "  - ESP32 OTA 地址:                                  http://$($env:HOST_LAN_IP):18003/xiaozhi/ota/" -ForegroundColor White
Write-Host "---------------------------------------------------------"
