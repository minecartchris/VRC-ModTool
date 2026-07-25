@echo off
REM Reports your VRChat instance roster to the mod server.
REM First run:  agent.bat --server https://your-server --token YOUR_TOKEN
REM After that: just double-click this file.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" agent.py %*
) else (
    python agent.py %*
)
if errorlevel 1 pause
