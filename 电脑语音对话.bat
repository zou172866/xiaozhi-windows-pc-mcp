@echo off
chcp 65001 >nul
if exist "%~dp0pc-mcp\.venv\Scripts\python.exe" (
    "%~dp0pc-mcp\.venv\Scripts\python.exe" -X utf8 "%~dp0scripts\pc_talk.py" --mic %*
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -X utf8 "%~dp0scripts\pc_talk.py" --mic %*
    ) else (
        python -X utf8 "%~dp0scripts\pc_talk.py" --mic %*
    )
)
if "%~1"=="" pause
