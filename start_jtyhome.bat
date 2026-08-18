@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "JTY_PYTHON=.venv\Scripts\python.exe"
) else (
  set "JTY_PYTHON=python"
)

"%JTY_PYTHON%" app.py --browser chrome %*
if errorlevel 1 pause
endlocal
