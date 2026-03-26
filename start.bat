@echo off
cd /d "%~dp0"
.\venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 4000
