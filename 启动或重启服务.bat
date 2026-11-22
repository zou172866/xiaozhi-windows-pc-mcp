@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0data\start_all.ps1"
if "%1"=="--no-pause" exit /b %ERRORLEVEL%
pause
