@echo off
title KAM Sentinel - Setup
color 0B

echo.
echo  ============================================
echo   KAM Sentinel - First Time Setup
echo  ============================================
echo.

:: Check for Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found!
    echo  Please install Python 3.8+ from https://python.org
    echo  Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo  [OK] Python found
echo  [..] Installing required packages...
echo.

pip install flask psutil GPUtil wmi pywin32 --quiet

if errorlevel 1 (
    echo.
    echo  [WARN] Some packages failed - GPU monitoring may be limited.
    echo  Core monitoring will still work.
) else (
    echo  [OK] All packages installed successfully
)

echo.
echo  [..] Creating backup directory...
if not exist "backups" mkdir backups
if not exist "logs" mkdir logs
if not exist "profiles" mkdir profiles

echo  [OK] Directories created
echo.
echo  ============================================
echo   Setup complete! Launching dashboard...
echo  ============================================
echo.

:: Launch the server and open browser
start "" http://localhost:5000
python server.py

pause
