@echo off
rem Double-click to open the To do list web page. Close this window to stop.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 todo_list.py) || (python todo_list.py)
pause
