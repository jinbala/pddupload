@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe -m pdd_listing_automation.app menu
pause
