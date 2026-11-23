function Test-ProjectPythonProcess {
    param($Process, [string]$ScriptPath, [string]$GsvConfig)
    if (-not $Process -or $Process.Name -notmatch '^python(w)?\.exe$') { return $false }
    $pythonPrefix = '^\s*(?:"[^"]+"|\S+)\s+(?:-X\s+utf8\s+)?'
    if ($GsvConfig) {
        # Older GSV launches use a relative script and this project's absolute -c argument.
        $script = '(?:api_v2\.py|[^"]*[/\\]api_v2\.py)'
        $cfg = [regex]::Escape($GsvConfig)
        return ($Process.CommandLine -match ($pythonPrefix + '(?:"' + $script + '"|api_v2\.py)(?:\s|$)') -and
                $Process.CommandLine -match ('\s-c\s+(?:"' + $cfg + '"|' + $cfg + ')(?:\s|$)'))
    }
    $script = [regex]::Escape($ScriptPath)
    return ($Process.CommandLine -match ($pythonPrefix + '(?:"' + $script + '"|' + $script + ')(?:\s|$)'))
}

function Stop-ProjectPythonProcess {
    param($Process, [string]$ScriptPath, [string]$GsvConfig)
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.ProcessId)" -ErrorAction SilentlyContinue
    if ($Process.CreationDate -and $current.CreationDate -eq $Process.CreationDate -and
        (Test-ProjectPythonProcess $current $ScriptPath $GsvConfig)) {
        Stop-Process -Id $current.ProcessId -Force -ErrorAction Stop
    }
}

function Test-ProjectListener {
    param([int]$Port, [string]$ScriptPath, [string]$GsvConfig)
    foreach ($listener in (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
        if (-not (Test-ProjectPythonProcess $owner $ScriptPath $GsvConfig)) { return $false }
    }
    return $true
}

function Get-DockerInstallRoots {
    param([string[]]$ExtraRoots = @())
    $roots = [System.Collections.Generic.List[string]]::new()
    foreach ($er in @($ExtraRoots)) {
        if ($er -and $er.Trim()) { $roots.Add($er.Trim()) }
    }
    foreach ($regPath in @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop"
    )) {
        try {
            if (Test-Path $regPath) {
                $item = Get-ItemProperty -Path $regPath -ErrorAction SilentlyContinue
                if ($item -and $item.InstallLocation) {
                    $roots.Add([string]$item.InstallLocation)
                }
            }
        } catch {}
    }
    foreach ($envBase in @($env:ProgramFiles, $env:ProgramW6432)) {
        if ($envBase) { $roots.Add((Join-Path $envBase "Docker\Docker")) }
    }
    if ($env:LOCALAPPDATA) {
        $roots.Add((Join-Path $env:LOCALAPPDATA "Docker"))
    }
    try {
        foreach ($drv in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
            if ($drv -and $drv.Root) {
                $roots.Add((Join-Path $drv.Root "Docker"))
                $roots.Add((Join-Path $drv.Root "Docker\Docker"))
                $roots.Add((Join-Path $drv.Root "Program Files\Docker\Docker"))
            }
        }
    } catch {}
    return @($roots | Where-Object { $_ } | Select-Object -Unique)
}

function Resolve-DockerCliPath {
    param(
        [string[]]$CandidateRoots = @(),
        [switch]$NoPathUpdate,
        [switch]$SkipExistingCommand
    )
    if (-not $SkipExistingCommand) {
        $existingCmd = Get-Command docker -ErrorAction SilentlyContinue
        if ($existingCmd -and $existingCmd.Source -and (Test-Path $existingCmd.Source)) {
            return [string]$existingCmd.Source
        }
    }
    $roots = if ($CandidateRoots -and $CandidateRoots.Count -gt 0) {
        @($CandidateRoots)
    } else {
        @(Get-DockerInstallRoots)
    }
    foreach ($root in $roots) {
        foreach ($rel in @("resources\bin\docker.exe", "bin\docker.exe", "docker.exe")) {
            $cand = Join-Path $root $rel
            if (Test-Path $cand) {
                $binDir = Split-Path -Parent $cand
                if (-not $NoPathUpdate -and $binDir) {
                    $pathEntries = @($env:Path -split ';' | ForEach-Object { $_.Trim().TrimEnd('\') } | Where-Object { $_ })
                    $normBin = $binDir.Trim().TrimEnd('\')
                    if ($pathEntries -notcontains $normBin) {
                        $env:Path = "$binDir;$env:Path"
                    }
                }
                return [string]$cand
            }
        }
    }
    return ""
}
