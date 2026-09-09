@echo off
REM ============================================================
REM  Start everything: open backend + frontend in two windows
REM  1) backend  -> http://127.0.0.1:8000  (API)
REM  2) frontend -> http://127.0.0.1:5173  (browser UI)
REM  Note: Qdrant & PostgreSQL must already run via docker-compose up -d
REM ============================================================
start "KBQA-Backend" cmd /k "%~dp0start-backend.bat"
start "KBQA-Frontend" cmd /k "%~dp0start-frontend.bat"
echo Both windows opened. Close them to stop the servers.
