@echo off
REM ============================================================
REM  选择启动领域
REM ------------------------------------------------------------
REM  双击本文件 → 用方向键选择业务领域 → 回车 → 自动启动。
REM
REM  实际逻辑在 scripts\choose-start.ps1。
REM  这里只做一件事：用正确的参数把 PowerShell 叫起来。
REM
REM  为什么带 -ExecutionPolicy Bypass：
REM    系统默认可能禁止运行未签名的 .ps1 脚本，
REM    不带这个参数用户会看到"无法加载脚本"的红字，一头雾水。
REM    本项目脚本都在本地、随项目分发，不是从网上下载的，风险可控。
REM ============================================================

chcp 65001 >nul 2>&1
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\choose-start.ps1"

if errorlevel 1 (
    echo.
    echo  启动过程出现错误，请查看上方提示。
    pause
)
