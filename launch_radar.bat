@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" goto missing_python
if not exist "web\dist\index.html" goto missing_web

start "" ".venv\Scripts\pythonw.exe" -B "%~dp0launch_radar.pyw" --open-web %*
exit /b %ERRORLEVEL%

:missing_python
echo [ResearchRadar] Run: uv sync --locked --extra ui --extra embedding
pause
exit /b 1

:missing_web
echo [ResearchRadar] Run in the web directory: npm ci ^&^& npm run build
pause
exit /b 1
