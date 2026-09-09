@echo off
REM ============================================================
REM  Start backend (FastAPI) on http://127.0.0.1:8000
REM  API docs:  http://127.0.0.1:8000/docs
REM ============================================================
setlocal
cd /d "%~dp0..\backend"

REM Use the project venv (created during setup). Fallback to system python.
set PYTHON=C:\Users\pjs\.venvs\kbqa\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python

echo Starting backend server ...
"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause
