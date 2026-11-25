@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0data\start_companion_widget.ps1" %*
if errorlevel 1 pause
