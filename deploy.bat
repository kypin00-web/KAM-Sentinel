@echo off
title KAM Sentinel - Deploy Pipeline
color 0B

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║   KAM Sentinel - Full Deploy Pipeline   ║
echo  ╚══════════════════════════════════════════╝
echo.

:: Step 1 - Pull latest
echo  [1/6] Pulling latest from GitHub...
git pull origin main
if errorlevel 1 (echo  [ERROR] Git pull failed & pause & exit /b 1)
echo  [OK] Up to date

:: Step 2 - Run full test suite
echo  [2/6] Running test suite...
python test_kam.py
if errorlevel 1 (
    echo.
    echo  [ERROR] Tests FAILED - aborting deploy
    echo  [INFO]  Check test_report.html for details
    pause & exit /b 1
)
echo  [OK] All tests passed

:: Step 3 - Install dependencies
echo  [3/6] Installing dependencies...
python -m pip install flask psutil GPUtil wmi pywin32 pyinstaller --quiet
echo  [OK] Dependencies ready

:: Step 4 - Build EXE
echo  [4/6] Building EXE...
python -m PyInstaller --name "KAM_Sentinel" --onefile --noconsole ^
  --icon "kam_sentinel.ico" ^
  --add-data "dashboard.html;." ^
  --add-data "thresholds.py;." ^
  --add-data "kam_sentinel.ico;." ^
  --hidden-import flask ^
  --hidden-import psutil ^
  --hidden-import wmi ^
  --hidden-import GPUtil ^
  launch.py
if errorlevel 1 (echo  [ERROR] Build failed & pause & exit /b 1)
echo  [OK] EXE built: dist\KAM_Sentinel.exe

:: Step 5 - Commit and push
echo  [5/6] Committing to GitHub...
git add .
for /f "tokens=*" %%v in ('python -c "import json; d=json.load(open(\"version.json\")); print(d[\"version\"])"') do set VERSION=%%v
git commit -m "v%VERSION% - Automated deploy build"
git push origin main
echo  [OK] Pushed to GitHub

:: Step 6 - Done
echo  [6/6] Deploy complete!
echo.
echo  ╔══════════════════════════════════════════╗
echo  ║   BUILD COMPLETE: v%VERSION%                  ║
echo  ║   EXE: dist\KAM_Sentinel.exe            ║
echo  ╚══════════════════════════════════════════╝
echo.
pause
