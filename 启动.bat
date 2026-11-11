@echo off
setlocal EnableDelayedExpansion
title KBQA-RAG 一键启动

REM ============================================================
REM  KBQA-RAG 一键启动
REM ------------------------------------------------------------
REM  双击本文件即可。按顺序完成：
REM    1) 检查 Docker / Python / 前端依赖
REM    2) 启动 PostgreSQL + Qdrant 容器并等它们健康
REM    3) 启动后端 (FastAPI, 8000) 并等它真正响应
REM    4) 启动前端 (Vue, 5173)
REM    5) 自动打开浏览器
REM
REM  已经启动过的部分会自动跳过。
REM  关闭请双击项目根目录的「停止.bat」。
REM ============================================================

cd /d "%~dp0"
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "PIDDIR=%ROOT%\.run"
if not exist "%PIDDIR%" mkdir "%PIDDIR%"

echo.
echo ============================================================
echo   KBQA-RAG 电商知识库问答系统 - 一键启动
echo ============================================================
echo.

REM ---------- 1) 基础环境检查 ----------
echo [1/6] 检查基础环境...

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
    echo.
    echo   [错误] 没有找到 Docker，请先安装并启动 Docker Desktop。
    echo.
    pause
    exit /b 1
)
echo   OK - Docker 已就绪

REM 找 Python：项目内 venv > PYTHON 环境变量 > 常见外部 venv > 系统 python
set "PYTHON_EXE="
if exist "%ROOT%\backend\.venv\Scripts\python.exe" set "PYTHON_EXE=%ROOT%\backend\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if defined PYTHON if exist "%PYTHON%" set "PYTHON_EXE=%PYTHON%"
if not defined PYTHON_EXE if exist "%USERPROFILE%\.venvs\kbqa\Scripts\python.exe" set "PYTHON_EXE=%USERPROFILE%\.venvs\kbqa\Scripts\python.exe"
if not defined PYTHON_EXE (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
)
if not defined PYTHON_EXE (
    echo.
    echo   [错误] 没有找到 Python，请安装 Python 3.11+ 并加入 PATH。
    echo.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -c "import fastapi, uvicorn, sqlalchemy" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [错误] 该 Python 缺少项目依赖：
    echo     "%PYTHON_EXE%"
    echo   请执行： pip install -r backend\requirements.txt
    echo.
    pause
    exit /b 1
)
echo   OK - Python 依赖完整

if not exist "%ROOT%\frontend\node_modules" (
    echo.
    echo   [错误] 前端依赖未安装，请先执行： cd frontend  然后  npm install
    echo.
    pause
    exit /b 1
)
echo   OK - 前端依赖已安装
echo.

REM ---------- 2) 数据库容器 ----------
echo [2/6] 启动数据库容器（PostgreSQL + Qdrant）...

if not exist "%ROOT%\.env" (
    if exist "%ROOT%\.env.example" (
        echo   未找到 .env，正在从 .env.example 复制...
        copy /y "%ROOT%\.env.example" "%ROOT%\.env" >nul
        echo   [注意] 请稍后打开 .env，把数据库密码改成你自己的。
    ) else (
        echo   [错误] 缺少 .env 与 .env.example。
        pause
        exit /b 1
    )
)

docker compose -f "%ROOT%\docker-compose.yml" up -d >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [错误] 数据库容器启动失败。
    echo   常见原因：Docker Desktop 还没完全启动。等它显示 Running 再试。
    echo.
    pause
    exit /b 1
)

set /a WAITED=0
:WAIT_DB
docker inspect --format "{{.State.Health.Status}}" kbqa-postgres 2>nul | findstr /i "healthy" >nul
if not errorlevel 1 goto DB_READY
set /a WAITED+=2
if !WAITED! GEQ 90 (
    echo   [警告] 数据库 90 秒仍未就绪，继续启动后端。
    goto DB_READY
)
ping -n 3 127.0.0.1 >nul
goto WAIT_DB
:DB_READY
echo   OK - 数据库容器已就绪
echo.

REM ---------- 3) 后端 ----------
echo [3/6] 启动后端（http://127.0.0.1:8000）...

netstat -ano | findstr /r /c:"TCP.*:8000 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   后端已在运行，跳过。
    goto BACKEND_DONE
)

REM 用 PowerShell 启动并把 PID 记到文件，停止时据此精确停止
REM （explorer 方式启动的窗口不依赖本脚本，脚本退出服务照常运行）
powershell -NoProfile -Command ^
  "$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/k','\"\"%PYTHON_EXE%\" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload\"' -WorkingDirectory '%ROOT%\backend' -PassThru; $p.Id | Out-File -Encoding ascii '%PIDDIR%\backend.pid'"
if errorlevel 1 (
    echo   [错误] 后端启动失败。
    pause
    exit /b 1
)

set /a WAITED=0
:WAIT_BACKEND
"%PYTHON_EXE%" -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live',timeout=2).status==200 else 1)" >nul 2>&1
if not errorlevel 1 goto BACKEND_DONE
set /a WAITED+=2
if !WAITED! GEQ 120 (
    echo   [警告] 后端 120 秒未就绪，请看弹出的后端窗口报错。
    goto BACKEND_DONE
)
ping -n 3 127.0.0.1 >nul
goto WAIT_BACKEND
:BACKEND_DONE
echo   OK - 后端已就绪
echo.

REM ---------- 4) 前端 ----------
echo [4/6] 启动前端（http://127.0.0.1:5173）...

netstat -ano | findstr /r /c:"TCP.*:5173 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   前端已在运行，跳过。
    goto FRONTEND_DONE
)

powershell -NoProfile -Command ^
  "$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/k','cd /d \"%ROOT%\frontend\" && npm run dev' -WorkingDirectory '%ROOT%\frontend' -PassThru; $p.Id | Out-File -Encoding ascii '%PIDDIR%\frontend.pid'"
if errorlevel 1 (
    echo   [错误] 前端启动失败。
    pause
    exit /b 1
)

set /a WAITED=0
:WAIT_FRONTEND
netstat -ano | findstr /r /c:"TCP.*:5173 .*LISTENING" >nul 2>&1
if not errorlevel 1 goto FRONTEND_DONE
set /a WAITED+=2
if !WAITED! GEQ 120 (
    echo   [警告] 前端 120 秒未就绪，请看弹出的前端窗口。
    goto FRONTEND_DONE
)
ping -n 3 127.0.0.1 >nul
goto WAIT_FRONTEND
:FRONTEND_DONE
echo   OK - 前端已就绪
echo.

REM ---------- 5) 打开浏览器 ----------
echo [5/6] 打开浏览器...
start "" "http://127.0.0.1:5173"
echo   OK
echo.

echo [6/6] 全部完成
echo.
echo ============================================================
echo   系统已启动
echo ------------------------------------------------------------
echo   使用地址： http://127.0.0.1:5173
echo   接口文档： http://127.0.0.1:8000/docs
echo.
echo   关闭系统： 双击项目根目录的「停止.bat」
echo ============================================================
echo.
echo 本窗口可以关掉，不影响已启动的服务。
ping -n 6 127.0.0.1 >nul
exit /b 0
