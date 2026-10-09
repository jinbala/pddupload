@echo off
cd /d "%~dp0"
set MULTIPLE=1.5
set DELAY=6
set /p MULTIPLE=Enter multiple (default 1.5): 
set /p DELAY=Enter delay seconds (default 6): 
.venv\Scripts\python.exe -m pdd_listing_automation.app publish-drafts --multiple %MULTIPLE% --delay-seconds %DELAY%
pause
