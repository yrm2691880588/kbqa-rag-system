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

REM ---------- 端口配置（可在根目录 .env 里覆盖）----------
REM 为什么要可配：你机器上可能同时跑着别的项目，占用了 8000 或 5173。
REM 本项目不去抢别人的端口，改成从 .env 读，不写死。
REM   BACKEND_PORT=8010
REM   FRONTEND_PORT=5180
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5173"
if exist "%ROOT%\.env" (
    for /f "usebackq tokens=1,2 delims==" %%A in ("%ROOT%\.env") do (
        if /i "%%A"=="BACKEND_PORT" if not "%%B"=="" set "BACKEND_PORT=%%B"
        if /i "%%A"=="FRONTEND_PORT" if not "%%B"=="" set "FRONTEND_PORT=%%B"
    )
)
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

REM ---------- 端口冲突预检 ----------
REM 背景：数据库端口很容易和「另一个项目的 PostgreSQL」撞车，
REM 撞车时报的是 "port is already allocated"，但上面把 compose 输出丢进了 nul，
REM 用户只会看到一句失败。所以先探一遍，撞车时说清楚原因和办法。
REM
REM 注意：检查的端口必须来自 .env 的 POSTGRES_HOST_PORT（默认 5432），
REM 不能写死 5432 —— 否则用户把端口改成 5433 之后，预检还去查 5432，
REM 反而会把正常启动给拦下来。
set "DB_PORT=5432"
if exist "%ROOT%\.env" (
    for /f "usebackq tokens=1,2 delims==" %%A in ("%ROOT%\.env") do (
        if /i "%%A"=="POSTGRES_HOST_PORT" if not "%%B"=="" set "DB_PORT=%%B"
    )
)
set "PORT_BUSY=0"
netstat -ano | findstr /r /c:"TCP.*:%DB_PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 set "PORT_BUSY=1"
if "!PORT_BUSY!"=="1" (
    REM 端口有占用：看是不是本项目容器自己占的（那就是正常的）
    set "PG_RUNNING=0"
    docker ps --filter "name=kbqa-postgres" --filter "status=running" --format "{{.Names}}" 2>nul | findstr "kbqa-postgres" >nul
    if not errorlevel 1 set "PG_RUNNING=1"
    if "!PG_RUNNING!"=="1" (
        echo   OK - 数据库容器已在运行（%DB_PORT% 正是它占的）
    ) else (
        echo.
        echo   [错误] 端口 %DB_PORT% 已被别的程序占用，本项目的数据库容器起不来。
        echo.
        echo   最常见原因：你机器上还有**另一个项目的 PostgreSQL** 在跑，
        echo   ^(或本机装了原生 PostgreSQL^)，它用的也是这个端口。
        echo.
        echo   解决办法（推荐第一种）：
        echo.
        echo     【一】给本项目换一个端口
        echo          1. 打开项目根目录的 .env
        echo          2. 加一行（或改现有值）：  POSTGRES_HOST_PORT=5433
        echo          3. 打开 backend\.env，把 DATABASE_URL 里的端口改成同一个数字
        echo             例如  @127.0.0.1:5432  改成  @127.0.0.1:5433
        echo          4. 重新双击本脚本
        echo.
        echo     【二】停掉占用 %DB_PORT% 的那个程序
        echo          先确认是谁：netstat -ano ^| findstr :%DB_PORT%
        echo          ^(如果那是别的项目，请不要直接关 Docker Desktop，
        echo           那会把其他项目的容器一起停掉^)
        echo.
        pause
        exit /b 1
    )
)


REM 注意：这里**不能**用 docker compose 的退出码判断成功与否。
REM 实测（Docker Compose v5）：当所有容器都已在运行、没有需要创建的资源时，
REM up -d 会返回退出码 1。第二次起服务时必然触发，
REM 若按退出码判断就会误报“启动失败”，让人以为 Docker 坏了。
REM 所以改为：不看返回码，直接核实两个容器是否真的在跑。
docker compose -f "%ROOT%\docker-compose.yml" up -d >nul 2>&1
set "UP_OK=1"
docker ps --filter "name=kbqa-postgres" --filter "status=running" --format "{{.Names}}" 2>nul | findstr "kbqa-postgres" >nul
if errorlevel 1 set "UP_OK=0"
docker ps --filter "name=kbqa-qdrant" --filter "status=running" --format "{{.Names}}" 2>nul | findstr "kbqa-qdrant" >nul
if errorlevel 1 set "UP_OK=0"
if "!UP_OK!"=="0" (
    echo.
    echo   [错误] 数据库容器没能进入运行状态。
    echo   常见原因：Docker Desktop 还没完全启动完成。
    echo   排查办法：在项目根目录执行下面这条命令看具体报错：
    echo       docker compose up -d
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
echo [3/6] 启动后端（端口 %BACKEND_PORT%）...

netstat -ano | findstr /r /c:"TCP.*:%BACKEND_PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   后端已在运行，跳过。
    goto BACKEND_DONE
)

REM 用 PowerShell 启动并把 PID 记到文件，停止时据此精确停止
REM （explorer 方式启动的窗口不依赖本脚本，脚本退出服务照常运行）
powershell -NoProfile -Command ^
  "$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/k','\"\"%PYTHON_EXE%\" -m uvicorn app.main:app --host 127.0.0.1 --port %BACKEND_PORT% --reload\"' -WorkingDirectory '%ROOT%\backend' -PassThru; $p.Id | Out-File -Encoding ascii '%PIDDIR%\backend.pid'"
if errorlevel 1 (
    echo   [错误] 后端启动失败。
    pause
    exit /b 1
)

set /a WAITED=0
:WAIT_BACKEND
"%PYTHON_EXE%" -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%BACKEND_PORT%/api/v1/health/live',timeout=2).status==200 else 1)" >nul 2>&1
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
echo [4/6] 启动前端（端口 %FRONTEND_PORT%）...

netstat -ano | findstr /r /c:"TCP.*:%FRONTEND_PORT% .*LISTENING" >nul 2>&1
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
netstat -ano | findstr /r /c:"TCP.*:%FRONTEND_PORT% .*LISTENING" >nul 2>&1
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
start "" "http://127.0.0.1:%FRONTEND_PORT%"
echo   OK
echo.

echo [6/6] 全部完成
echo.
echo ============================================================
echo   系统已启动
echo ------------------------------------------------------------
echo   使用地址： http://127.0.0.1:%FRONTEND_PORT%
echo   接口文档： http://127.0.0.1:%BACKEND_PORT%/docs
echo.
echo   关闭系统： 双击项目根目录的「停止.bat」
echo ============================================================
echo.
echo 本窗口可以关掉，不影响已启动的服务。
ping -n 6 127.0.0.1 >nul
exit /b 0
