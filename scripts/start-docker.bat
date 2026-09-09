@echo off
REM ============================================================
REM  Start Docker services (Qdrant + PostgreSQL)
REM  If your Docker is not at D:\DockerDesktop, edit DOCKER_BIN below.
REM ============================================================
setlocal
cd /d "%~dp0.."

set "DOCKER_BIN=D:\DockerDesktop\resources\bin"
set "DOCKER_COMPOSE=docker-compose.exe"

REM Ensure a local .env exists (docker compose reads DB password from it).
REM .env is gitignored; copy from the committed .env.example template if missing.
if not exist ".env" (
    echo No .env found - copying .env.example to .env ...
    copy /y ".env.example" ".env" >nul
)

REM Prefer the bundled docker-compose next to Docker Desktop; fall back to PATH.
if exist "%DOCKER_BIN%\docker-compose.exe" (
    set "PATH=%DOCKER_BIN%;%PATH%"
    set "DOCKER_COMPOSE=%DOCKER_BIN%\docker-compose.exe"
)

echo Starting Qdrant + PostgreSQL containers ...
"%DOCKER_COMPOSE%" up -d
echo.
echo Container status:
"%DOCKER_COMPOSE%" ps
pause
