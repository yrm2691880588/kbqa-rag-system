@echo off
REM ============================================================
REM  导出领域项目
REM ------------------------------------------------------------
REM  双击本文件 → 方向键多选领域 → 空格勾选 → 回车 → 选择位置 → 自动导出。
REM
REM  实际逻辑在 scripts\choose-export.ps1。
REM ============================================================

chcp 65001 >nul 2>&1
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\choose-export.ps1"

if errorlevel 1 (
    echo.
    echo  导出过程出现错误，请查看上方提示。
    pause
)
