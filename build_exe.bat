@echo off
title KAM Sentinel - Build EXE
color 0B

echo.
echo  ============================================
echo   KAM Sentinel v1.5.2 - Building Portable EXE
echo  ============================================
echo.

echo  [..] Running tests first...
python test_kam.py
if errorlevel 1 (
    echo  [ERROR] Tests failed - fix before building!
    pause & exit /b 1
)
echo  [OK] Tests passed!

echo  [..] Installing pyinstaller...
python -m pip install pyinstaller --quiet

echo  [..] Building EXE - this takes 1-2 minutes...
python -m PyInstaller ^
  --name "KAM_Sentinel" ^
  --onefile ^
  --noconsole ^
  --icon "kam_sentinel.ico" ^
  --add-data "dashboard.html;." ^
  --add-data "thresholds.py;." ^
  --add-data "kam_sentinel.ico;." ^
  --hidden-import flask ^
  --hidden-import psutil ^
  --hidden-import wmi ^
  --hidden-import GPUtil ^
  launch.py

if errorlevel 1 (
    echo  [ERROR] Build failed - check output above
    pause & exit /b 1
)

echo.
echo  ============================================
echo   BUILD COMPLETE!
echo   EXE is at: dist\KAM_Sentinel.exe
echo  ============================================
echo.
pause
