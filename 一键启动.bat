@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 未检测到运行环境，请先双击「安装环境.bat」完成初始化。
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m pdd_listing_automation.app menu
pause
