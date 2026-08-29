@echo off
cd /d %~dp0
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
