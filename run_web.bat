@echo off
rem Double-click to open the mail dashboard in your browser. Close this window to stop it.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 run_web.py %*) || (python run_web.py %*)
pause
