@echo off
setlocal
title KBQA-RAG 停止数据库容器

REM ============================================================
REM  停止数据库容器（PostgreSQL + Qdrant）
REM ------------------------------------------------------------
REM  · 只停容器，不动前后端进程
REM  · 数据卷会保留，下次启动数据还在
REM  · 要"连前后端一起停"，请用根目录的「停止.bat」
REM ============================================================

cd /d "%~dp0.."

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
    echo [ERROR] Docker not found. Start Docker Desktop first.
    pause
    exit /b 1
)

echo Stopping PostgreSQL + Qdrant containers ...
docker compose down
if errorlevel 1 (
    echo [WARN] docker compose down returned an error.
) else (
    echo Done. Data volumes are kept - your data will still be there next time.
)
echo.
pause
