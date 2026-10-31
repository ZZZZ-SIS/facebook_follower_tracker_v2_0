@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.10+ not found.
  pause
  exit /b 1
)

rem Install only the pinned dependency set when runtime packages are missing.
py -c "import PyQt6, playwright, google.auth, requests, urllib3" >nul 2>nul
if errorlevel 1 (
  echo Missing runtime packages. Installing the pinned dependency set...
  py -m pip install --disable-pip-version-check --only-binary=:all: -r requirements.txt -c constraints-security.txt
  if errorlevel 1 (
    echo [ERROR] Failed to install required packages.
    echo Please run install_windows.bat and check your network / Python environment.
    pause
    exit /b 1
  )
)

py -m pip check >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python dependency conflicts detected. Run install_windows.bat.
  pause
  exit /b 1
)

py facebook_follower_tracker_v2_0.py
if errorlevel 1 pause
