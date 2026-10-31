@echo off
setlocal
cd /d "%~dp0"

REM Security: do not add --uac-admin. This application must run as a normal user.

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.10+ not found.
  pause
  exit /b 1
)

py -m pip install --disable-pip-version-check --only-binary=:all: -r requirements-build.txt -c constraints-security.txt
if errorlevel 1 pause & exit /b 1
py -m pip check
if errorlevel 1 pause & exit /b 1

py -m PyInstaller --noconfirm --clean --onefile --windowed --name FacebookFollowerTracker --collect-all playwright --collect-all google.auth --collect-all requests --add-data "assets;assets" --icon "assets\app_icon.ico" facebook_follower_tracker_v2_0.py
if errorlevel 1 pause & exit /b 1
echo.
echo Build complete: dist\FacebookFollowerTracker.exe
pause
