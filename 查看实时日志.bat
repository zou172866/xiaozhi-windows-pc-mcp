@echo off
where docker >nul 2>nul
if errorlevel 1 (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ". '%~dp0data\host_process.ps1'; [void](Resolve-DockerCliPath); docker logs -f --tail 80 xiaozhi-esp32-server"
) else (
    docker logs -f --tail 80 xiaozhi-esp32-server
)
pause
