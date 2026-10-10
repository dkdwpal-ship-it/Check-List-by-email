@echo off
rem Double-click to open the mail to-do web page. Close this window to stop.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 mail_web.py) || (python mail_web.py)
pause
