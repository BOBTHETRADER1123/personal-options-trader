@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\backup.py restore
pause
