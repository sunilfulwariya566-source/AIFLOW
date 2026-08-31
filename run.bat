@echo off
REM AIFlow launcher for Windows. Double-click karo ya cmd me: run.bat
cd /d "%~dp0"

if "%PORT%"=="" set PORT=8000

where python >nul 2>nul
if errorlevel 1 (
  echo Python nahi mila. Install karo: https://www.python.org/downloads/
  echo Install karte waqt "Add Python to PATH" zaroor tick karna.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment ^(one time^)...
  python -m venv .venv
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\pip install --quiet -r requirements.txt
  echo Dependencies installed.
)

echo.
echo   AIFlow chal raha hai:  http://localhost:%PORT%
echo   Rokne ke liye Ctrl+C dabao
echo.

.venv\Scripts\python -m uvicorn app:app --host 0.0.0.0 --port %PORT%
pause
