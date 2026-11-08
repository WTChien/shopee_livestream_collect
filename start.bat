@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Install the project environment first.
  pause
  exit /b 1
)
set "PYTHONIOENCODING=utf-8"
if exist "line.env" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-line.ps1"
".venv\Scripts\python.exe" local_app.py
if errorlevel 1 (
  echo Local dashboard failed to start.
  pause
)
