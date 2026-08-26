@echo off
setlocal
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem quant-agent one-click launcher
rem Usage:  start.bat [port]      (default port: 8643)
rem ------------------------------------------------------------------

set PORT=%1
if "%PORT%"=="" set PORT=8643

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" (
    echo [ERROR] Python environment not found.
    echo         Install Python 3.10+, then run:
    echo             python -m venv .venv
    echo             .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo See README_STARTUP.md for details.
    pause
    exit /b 1
)

echo ==============================================
echo  quant-agent  -  http://127.0.0.1:%PORT%
echo  Python : %PY%
echo  Stop   : close this window, or run stop.bat
echo ==============================================

rem open default browser ~2s later (after server is up)
start "" /min cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:%PORT%"

"%PY%" -m uvicorn app:app --host 127.0.0.1 --port %PORT%

echo.
echo Server stopped.
pause
