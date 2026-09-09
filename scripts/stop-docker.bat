@echo off
REM ============================================================
REM  Stop Docker containers (PostgreSQL + Qdrant)
REM  Double-click to stop and remove kbqa containers.
REM  Note: data in Docker volumes is preserved.
REM ============================================================
setlocal
cd /d "%~dp0.."

set "DOCKER_BIN=D:\DockerDesktop\resources\bin"

if exist "%DOCKER_BIN%\docker-compose.exe" (
    set "PATH=%DOCKER_BIN%;%PATH%"
    echo Stopping Qdrant + PostgreSQL containers ...
    "%DOCKER_BIN%\docker-compose.exe" down
) else (
    echo docker-compose not found, please start Docker Desktop first.
)
echo.
pause
