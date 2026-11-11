@echo off
setlocal
title KBQA-RAG 启动前端

REM ============================================================
REM  启动前端（单独执行本步）
REM ------------------------------------------------------------
REM  · 只做一件事：起 Vue 开发服务器（5173）
REM  · 日常使用建议直接用根目录的「启动.bat」，它会自动按顺序做完所有步骤
REM ============================================================

cd /d "%~dp0..\frontend"

if not exist "node_modules" (
    echo [ERROR] Frontend dependencies not installed.
    echo Please run:
    echo     cd frontend
    echo     npm install
    pause
    exit /b 1
)

echo Starting frontend dev server on http://127.0.0.1:5173 ...
call npm run dev
pause
