@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0data\stop_all.ps1"
if "%1"=="--no-pause" exit /b 0
pause
