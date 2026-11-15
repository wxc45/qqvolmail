@echo off
setlocal
cd /d "%~dp0"

where python.exe >nul 2>nul
if errorlevel 1 (
    echo.
    echo Python 3 was not found. Please install Python 3.10 or newer.
    echo.
    pause
    exit /b 1
)

python.exe "%~dp0qqvolmail.py" %*
if errorlevel 1 pause
