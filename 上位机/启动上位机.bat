@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "HOST_EXIT=%errorlevel%"
if not "%HOST_EXIT%"=="0" (
    echo Workbench exited with an error. Read the output above.
    pause
)
exit /b %HOST_EXIT%
