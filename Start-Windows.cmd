@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto setup_needed
if not exist "frontend\dist\index.html" goto setup_needed
echo.
echo Personal Options Trader - PAPER ORDERS ONLY
echo.
echo Open this address in your browser: http://127.0.0.1:8000
echo Keep this window open while using the local app.
echo Press Ctrl+C to stop. Local recording stops when this window closes.
echo For access away from this laptop, follow docs\DEPLOYMENT.md.
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
if errorlevel 1 (
  echo.
  echo The app could not start. See the error above and START-HERE.md.
  pause
)
exit /b
:setup_needed
echo Run Setup-Windows.cmd first, then return here.
pause
exit /b 1
