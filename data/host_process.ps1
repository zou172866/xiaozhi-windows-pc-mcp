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
