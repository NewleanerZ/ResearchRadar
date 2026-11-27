@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ResearchRadar] 未检测到 .venv 虚拟环境，请先执行: uv sync --extra ui --extra embedding
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" -B "%~dp0launch_radar.pyw" --open-web %*
endlocal
