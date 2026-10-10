@echo off
setlocal
cd /d "%~dp0"
py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.12 is required. Install it from python.org and enable the Python launcher.
  pause
  exit /b 1
)
py -3.12 -c "import fastapi, ee, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo Installing project dependencies...
  py -3.12 -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. See README.md for help.
    pause
    exit /b 1
  )
)
echo Starting AgniWatch AI in offline demo-capable mode.
echo Open http://127.0.0.1:8000 and click Load usable demo dataset.
py -3.12 -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
