@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (
  echo [JTYHome] Python 3 not found. Please install Python 3 first.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (
  echo [JTYHome] Dependency installation failed.
  pause
  exit /b 1
)
echo.
echo [JTYHome] Installed. Starting...
call start_jtyhome.bat
endlocal
