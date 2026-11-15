@echo off
setlocal
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0launch_gui.pyw"
    exit /b 0
)

where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0launch_gui.pyw"
    exit /b 0
)

where python.exe >nul 2>nul
if not errorlevel 1 (
    python.exe "%~dp0launch_gui.pyw"
    exit /b 0
)

where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3 "%~dp0launch_gui.pyw"
    exit /b 0
)

echo.
echo Python 3 was not found.
echo Please install Python 3.10 or newer from https://www.python.org/downloads/
echo and enable "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1
