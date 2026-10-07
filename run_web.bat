@echo off
rem 더블클릭하면 메일 업로드 화면이 브라우저로 열립니다. 이 창을 닫으면 종료됩니다.
chcp 65001 > nul
cd /d "%~dp0"
python -m email_task_agent.web %*
pause
