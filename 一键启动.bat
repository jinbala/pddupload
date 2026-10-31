@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 未找到项目运行环境：.venv\Scripts\python.exe，请检查 .venv 目录是否完整。
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m pdd_listing_automation.app menu
pause
