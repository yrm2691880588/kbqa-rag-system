@echo off
REM ============================================================
REM  Start backend (FastAPI) on http://127.0.0.1:8000
REM  API docs:  http://127.0.0.1:8000/docs
REM ============================================================
setlocal
cd /d "%~dp0..\backend"

REM Python lookup order:
REM   1) PYTHON env var (if the caller already set one)
REM   2) project-local venv  backend\.venv
REM   3) system python on PATH
REM No absolute local paths are hard-coded, so this works on any machine.
if "%PYTHON%"=="" set PYTHON=%~dp0..\backend\.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python

echo Starting backend server ...
"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause
