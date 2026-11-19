@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "URL_FILE=%~dp0data\anima\studio_url.txt"
if not exist "%URL_FILE%" (
    echo [提示] 尚未检测到 Companion Studio 访问令牌，请先确认 Docker 服务已启动！
    pause
    exit /b 1
)
set /p STUDIO_URL=<"%URL_FILE%"
echo 正在用默认浏览器打开 Companion Studio 网页控制台...
start "" "%STUDIO_URL%"
