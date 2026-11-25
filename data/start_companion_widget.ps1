$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$serverDir = Split-Path -Parent $PSScriptRoot
Set-Location $serverDir
. (Join-Path $PSScriptRoot "host_process.ps1")

$widgetScript = Join-Path $serverDir "scripts\desktop_companion.py"
if (-not (Test-Path $widgetScript)) {
    Write-Host "[错误] 未找到桌角陪伴小窗脚本: $widgetScript" -ForegroundColor Red
    exit 1
}

$existing = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { Test-ProjectPythonProcess $_ $widgetScript })
if ($existing.Count -gt 0) {
    Write-Host "[提示] 桌角陪伴小窗已在运行中 (PID=$($existing[0].ProcessId))，无需重复启动。" -ForegroundColor Yellow
    exit 0
}

$pyw = Join-Path $serverDir "pc-mcp\.venv\Scripts\pythonw.exe"
$py = Join-Path $serverDir "pc-mcp\.venv\Scripts\python.exe"
$exe = if (Test-Path $pyw) { $pyw } elseif (Test-Path $py) { $py } else { "" }
if (-not $exe) {
    Write-Host "[错误] 未找到 pc-mcp\.venv Python 环境，请先运行「一键初始化配置.bat」。" -ForegroundColor Red
    exit 1
}

$cmdLine = "`"$exe`" -X utf8 `"$widgetScript`""
$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0; WinstationDesktop = "WinSta0\Default" }
$res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; CurrentDirectory = $serverDir; ProcessStartupInformation = $si }
if ($res.ReturnValue -ne 0) {
    Write-Host "[错误] 无法启动桌角陪伴小窗 (WMI ReturnValue=$($res.ReturnValue))" -ForegroundColor Red
    exit 1
}

Write-Host "[完成] 已无黑框启动桌角陪伴小窗 (PID=$($res.ProcessId))；双击头像可折叠，右键菜单可退出或看屏。" -ForegroundColor Green
exit 0
