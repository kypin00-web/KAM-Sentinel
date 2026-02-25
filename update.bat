@echo off
title KAM Sentinel - Update & Build
color 0B

echo.
echo  ============================================
echo   KAM Sentinel - Update from GitHub
echo  ============================================
echo.

:: Check git
git --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Git not found. Install from git-scm.com
    pause & exit /b 1
)

:: Pull latest from GitHub
echo  [..] Pulling latest files from GitHub...
git pull origin main
if errorlevel 1 (
    echo  [ERROR] Git pull failed. Check your connection.
    pause & exit /b 1
)
echo  [OK] Files updated from GitHub

echo.
echo  [..] Running test suite...
python test_kam.py
if errorlevel 1 (
    echo.
    echo  [ERROR] Tests FAILED after update!
    echo  [INFO]  Your old files are still running - nothing was broken.
    pause & exit /b 1
)
echo  [OK] All tests passed!

echo.
echo  ============================================
echo   Update complete! Restart server to apply.
echo  ============================================
echo.
echo  To restart the server:
echo  1. Press Ctrl+C in the server window
echo  2. Run: python server.py
echo.
echo  Or run build_exe.bat to build a new .exe
echo.
pause
