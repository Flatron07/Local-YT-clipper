@echo off
setlocal
cd /d "%~dp0"
title Local Clipper Setup

echo.
echo  ========================================
echo          LOCAL CLIPPER - STARTUP
echo  ========================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found.
        echo Install Python 3.11 or newer from https://www.python.org/downloads/
        echo Make sure to tick ^"Add Python to PATH^" during setup.
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo Local Clipper needs Python 3.11 or newer for the current yt-dlp release.
    echo Install Python 3.11+ from https://www.python.org/downloads/ and run start.bat again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a local Python environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
)

echo Installing or updating app dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Starting Local Clipper...
echo Keep this window open while using the app.
echo Your browser will open at http://127.0.0.1:5050
start "" http://127.0.0.1:5050
.venv\Scripts\python.exe app.py
pause
exit /b 0

:error
echo.
echo Setup failed. Check your internet connection and Python installation, then run start.bat again.
pause
exit /b 1
