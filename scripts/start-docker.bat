@echo off
setlocal
title KBQA-RAG 启动数据库容器

REM ============================================================
REM  启动数据库容器（PostgreSQL + Qdrant）
REM ------------------------------------------------------------
REM  · 只做一件事：起两个容器并等它们健康
REM  · 日常使用建议直接用根目录的「启动.bat」，它会自动按顺序做完所有步骤
REM  · Docker 路径自动探测，不写死（PATH -> Docker Desktop 默认位置）
REM ============================================================

cd /d "%~dp0.."

REM 找 docker：PATH 优先，找不到再看 Docker Desktop 默认安装位置
where docker >nul 2>&1
if errorlevel 1 (
    if exist "D:\DockerDesktop\resources\bin\docker.exe" (
        set "PATH=D:\DockerDesktop\resources\bin;%PATH%"
    ) else if exist "%ProgramFiles%\Docker\Docker\resources\bin\docker.exe" (
        set "PATH=%ProgramFiles%\Docker\Docker\resources\bin;%PATH%"
    )
)
where docker >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker not found.
    echo Please install and start Docker Desktop first.
    pause
    exit /b 1
)

REM docker compose 需要根目录 .env 里的数据库口令
if not exist ".env" (
    if exist ".env.example" (
        echo No .env found - copying .env.example to .env ...
        copy /y ".env.example" ".env" >nul
        echo [NOTE] Please open .env and change the database password.
    ) else (
        echo [ERROR] Missing .env and .env.example.
        pause
        exit /b 1
    )
)

echo Starting PostgreSQL + Qdrant containers ...
docker compose up -d
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to start containers.
    echo If Docker Desktop is still starting up, wait until it shows Running and retry.
    pause
    exit /b 1
)

echo.
echo Waiting for containers to become healthy ...
set /a WAITED=0
:WAIT_DB
docker inspect --format "{{.State.Health.Status}}" kbqa-postgres 2>nul | findstr /i "healthy" >nul
if not errorlevel 1 goto DB_READY
set /a WAITED+=2
if %WAITED% GEQ 90 (
    echo [WARN] Not healthy after 90s. Check: docker compose ps
    goto DB_READY
)
ping -n 3 127.0.0.1 >nul
goto WAIT_DB
:DB_READY

echo.
echo Container status:
docker compose ps
echo.
echo Ready. Next step: run scripts\start-backend.bat (or use the root 启动.bat).
pause
