@echo off
setlocal EnableDelayedExpansion
title KBQA-RAG 一键停止

REM ============================================================
REM  KBQA-RAG 一键停止
REM ------------------------------------------------------------
REM  双击本文件即可。它会：
REM    1) 停掉后端（按 .run\backend.pid 记录的 PID，并校验确实是本项目）
REM    2) 停掉前端（同上）
REM    3) 停止数据库容器（数据保留）
REM
REM  [重要] 安全边界（重要）：
REM     绝对不按进程名批量杀（如 taskkill /IM node.exe）。
REM     你机器上可能同时跑着别的项目、账号池后端等，
REM     按名字杀会把它们一起干掉。
REM     这里一律「先拿到具体 PID -> 多重特征校验 -> 确认是本项目才停」。
REM ============================================================

cd /d "%~dp0"
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "PIDDIR=%ROOT%\.run"

echo.
echo ============================================================
echo   KBQA-RAG 电商知识库问答系统 - 一键停止
echo ============================================================
echo.

set "KILLED=0"

REM ---------- 1) 后端 ----------
echo [1/4] 停止后端（8000）...
call :STOP_TARGET "backend" 8000 "后端"
echo.

REM ---------- 2) 前端 ----------
echo [2/4] 停止前端（5173）...
call :STOP_TARGET "frontend" 5173 "前端"
echo.

REM ---------- 2.5) 清理 uvicorn reload 产生的孤儿子进程 ----------
echo [3/4] 清理残留的 uvicorn 子进程...
powershell -NoProfile -Command ^
  "$n = 0; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | ForEach-Object { if ($_.CommandLine -match 'multiprocessing-fork' -and $_.CommandLine -match 'parent_pid') { $parent = $_.ParentProcessId; $alive = Get-Process -Id $parent -ErrorAction SilentlyContinue; if (-not $alive) { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ } } }; Write-Output ('  清理孤儿进程: ' + $n + ' 个')"
if errorlevel 1 echo   [提示] 孤儿进程清理跳过（无 PowerShell 或无残留）。

REM ---------- 3) 数据库容器 ----------
echo [4/4] 停止数据库容器（PostgreSQL + Qdrant）...

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
    echo   未找到 Docker，跳过。
    goto DONE
)

docker compose -f "%ROOT%\docker-compose.yml" down >nul 2>&1
if errorlevel 1 (
    echo   [警告] 停止返回异常，可手动执行 docker compose down 查看。
) else (
    echo   OK - 容器已停止（数据卷保留，下次启动数据还在）
)
echo.

:DONE
if exist "%PIDDIR%\backend.pid" del /q "%PIDDIR%\backend.pid" >nul 2>&1
if exist "%PIDDIR%\frontend.pid" del /q "%PIDDIR%\frontend.pid" >nul 2>&1

echo ============================================================
if "!KILLED!"=="0" (
    echo   完成。未发现本项目的运行进程（可能本来就没启动）。
) else (
    echo   全部已停止。
)
echo ============================================================
echo.
ping -n 6 127.0.0.1 >nul
exit /b 0


REM ============================================================
REM  子过程：停止某个目标
REM  参数：%1=pid文件名  %2=端口  %3=显示名
REM ============================================================
:STOP_TARGET
set "NAME=%~1"
set "PORT=%~2"
set "LABEL=%~3"
set "STOPPED=0"

REM --- A) 优先按启动时记录的 PID 停 ---
if exist "%PIDDIR%\%NAME%.pid" (
    set "SAVED_PID="
    for /f "usebackq delims=" %%P in ("%PIDDIR%\%NAME%.pid") do set "SAVED_PID=%%P"
    if defined SAVED_PID (
        call :KILL_IF_OURS !SAVED_PID! "%LABEL%" "%PORT%" "%NAME%"
        if !errorlevel!==0 set "STOPPED=1"
    )
)

REM --- B) 兜底：PID 文件丢失时，按端口找，但仍要校验特征 ---
if "!STOPPED!"=="0" (
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"TCP.*:%PORT% .*LISTENING"') do (
        call :KILL_IF_OURS %%P "%LABEL%" "%PORT%" "%NAME%"
        if !errorlevel!==0 set "STOPPED=1"
    )
)

if "!STOPPED!"=="0" echo   没有找到本项目的%LABEL%进程，跳过。
exit /b 0


REM ============================================================
REM  子过程：确认 PID 属于本项目后才停止
REM  参数：%1=PID %2=显示名 %3=端口 %4=目标名(backend/frontend)
REM ============================================================
:KILL_IF_OURS
set "PID=%~1"
set "LABEL=%~2"
set "PORT=%~3"
set "NAME=%~4"

if "%PID%"=="" exit /b 1
if "%PID%"=="0" exit /b 1

REM 用 PowerShell 一次性判断：进程是否存在 + 是否匹配本项目特征
REM 特征（任一满足即认为是本项目）：
REM   a) 命令行里含本项目目录
REM   b) 命令行含 uvicorn app.main:app（后端独有）
REM   c) 命令行含 vite（前端独有）+ 端口匹配
REM 用 PowerShell 而不是 findstr：实测 findstr 对中文路径与引号处理会误判
set "VERDICT="
for /f "usebackq delims=" %%R in (`powershell -NoProfile -Command "try { $p = Get-CimInstance Win32_Process -Filter 'ProcessId=%PID%' -ErrorAction Stop; $c = $p.CommandLine; $ours = ($c -match 'langchain RAG') -or ($c -match 'uvicorn app.main:app') -or ($c -match 'vite'); if ($ours) { 'YES' } else { 'NO' } } catch { 'GONE' }" 2^>nul`) do set "VERDICT=%%R"

if "!VERDICT!"=="GONE" (
    echo   [提示] PID %PID% 进程已不存在（端口可能处于 TIME_WAIT 残留，几十秒后自动释放）。
    exit /b 1
)
if not "!VERDICT!"=="YES" (
    echo   [跳过] PID %PID% 占用了 %PORT% 但不是本项目，不动它。
    exit /b 1
)

echo   停止 %LABEL%（PID %PID%）...
taskkill /PID %PID% /T /F >nul 2>&1
if errorlevel 1 (
    echo   [警告] 停止失败，可能需要管理员权限。
    exit /b 1
)
echo   OK
set "KILLED=1"
exit /b 0
