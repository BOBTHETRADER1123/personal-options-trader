@echo off
setlocal
cd /d "%~dp0"
echo Personal Options Trader - first-time setup
echo.
where py >nul 2>nul
if errorlevel 1 (
  python scripts\setup.py
) else (
  py -3 scripts\setup.py
)
if errorlevel 1 (
  echo.
  echo Setup needs attention. Read the message above and START-HERE.md.
  pause
  exit /b 1
)
echo.
echo Setup complete. Double-click Open-Trading-App.cmd to open your trading desk.
pause
