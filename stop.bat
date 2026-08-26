@echo off
setlocal

rem Stop the quant-agent server listening on the given port (default 8643)
set PORT=%1
if "%PORT%"=="" set PORT=8643

set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    if not "%%p"=="0" (
        echo Killing PID %%p on port %PORT% ...
        taskkill /PID %%p /F >nul 2>&1
        set FOUND=1
    )
)

if "%FOUND%"=="0" echo No process found on port %PORT%.
pause
