@echo off
REM ============================================================
REM  Start frontend (Vue3 dev server) on http://127.0.0.1:5173
REM ============================================================
setlocal
cd /d "%~dp0..\frontend"

echo Starting frontend dev server ...
call npm run dev
pause
