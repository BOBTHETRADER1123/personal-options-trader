@echo off
setlocal
cd /d "%~dp0"
if not exist "..\.venv\Scripts\python.exe" (
  echo Please run the main Windows setup script first.
  pause
  exit /b 1
)
if not exist ".env" (
  "..\.venv\Scripts\python.exe" configure.py
  if errorlevel 1 goto failed
)
"..\.venv\Scripts\python.exe" companion.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Companion could not start. See the message above and docs\EXPORTS.md.
pause
exit /b 1
