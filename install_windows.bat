@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.10+ not found. Please install a supported 64-bit Python first.
  pause
  exit /b 1
)

echo Installing pinned runtime dependencies...
py -m pip install --disable-pip-version-check --only-binary=:all: -r requirements.txt -c constraints-security.txt
if errorlevel 1 (
  echo [ERROR] Package installation failed.
  pause
  exit /b 1
)

py -m pip check
if errorlevel 1 (
  echo [ERROR] Dependency consistency check failed.
  pause
  exit /b 1
)

echo.
echo Runtime check:
py -c "import PyQt6, playwright, google.auth, requests, urllib3; print('Runtime dependencies OK')"
if errorlevel 1 (
  echo [ERROR] Packages were installed but cannot be imported by this Python.
  pause
  exit /b 1
)

echo.
echo Done. This version uses your installed Chrome/Edge; Playwright Chromium download is not required.
pause
