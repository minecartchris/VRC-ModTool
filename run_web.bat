@echo off
cd /d "%~dp0"
call .venv\Scripts\activate
python run_web.py %*
