@echo off
setlocal
title KBQA-RAG 请使用根目录的一键脚本

REM ============================================================
REM  本脚本已废弃（保留仅为兼容旧习惯）
REM ------------------------------------------------------------
REM  为什么不保留原功能：
REM    原来的 start-all.bat 只开前后端两个窗口，**不启动 Docker**。
REM    用户双击后以为"全都起来了"，结果后端连不上数据库而报错，
REM    还以为是项目坏了。这类"看着像是全自动、其实少一步"的脚本，
REM    比没有更糟。
REM
REM  现在请使用项目根目录的：
REM    启动.bat  —— 按正确顺序起容器 -> 后端 -> 前端 -> 开浏览器
REM    停止.bat  —— 一键全停
REM ============================================================

echo.
echo ============================================================
echo   This script is DEPRECATED.
echo.
echo   Please use the one-click scripts in the project ROOT folder:
echo.
echo     启动.bat  (start everything, in the right order)
echo     停止.bat  (stop everything)
echo.
echo   The old start-all.bat did NOT start Docker containers,
echo   which caused confusing "database connection failed" errors.
echo ============================================================
echo.

REM 如果根目录的 启动.bat 存在，直接帮用户转过去
if exist "%~dp0..\启动.bat" (
    choice /c YN /m "Run the root 启动.bat now"
    if errorlevel 2 goto DONE
    call "%~dp0..\启动.bat"
    goto DONE
)

:DONE
echo.
pause
