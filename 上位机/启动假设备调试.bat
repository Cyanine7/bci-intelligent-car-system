@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-automation.ps1" -Demo
exit /b %errorlevel%
