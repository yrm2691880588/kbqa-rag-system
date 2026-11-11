@echo off
setlocal
title KBQA-RAG 启动后端

REM ============================================================
REM  启动后端（单独执行本步）
REM ------------------------------------------------------------
REM  · 只做一件事：起 FastAPI（8000）
REM  · 前提：数据库容器已启动（用 scripts\start-docker.bat 或根目录 启动.bat）
REM  · 日常使用建议直接用根目录的「启动.bat」，它会自动按顺序做完所有步骤
REM ============================================================

cd /d "%~dp0..\backend"

REM 找 Python：项目内 venv > PYTHON 环境变量 > 常见外部 venv > 系统 python
set "PYTHON_EXE="
if exist "%~dp0..\backend\.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0..\backend\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if defined PYTHON set "PYTHON_EXE=%PYTHON%"
if not defined PYTHON_EXE if exist "%USERPROFILE%\.venvs\kbqa\Scripts\python.exe" set "PYTHON_EXE=%USERPROFILE%\.venvs\kbqa\Scripts\python.exe"
if not defined PYTHON_EXE (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
)
if not defined PYTHON_EXE (
    echo [ERROR] Python not found. Please install Python 3.11+ and add it to PATH.
    pause
    exit /b 1
)

echo Starting backend on http://127.0.0.1:8000 ...
echo API docs: http://127.0.0.1:8000/docs
echo.
"%PYTHON_EXE%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause
