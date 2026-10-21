@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\desktop_launcher.py --stop
pause
