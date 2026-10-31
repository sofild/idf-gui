@echo off
chcp 65001 >nul
title ESP-IDF 可视化工具
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    if exist "C:\ProgramData\anaconda3\python.exe" set "PY=C:\ProgramData\anaconda3\python.exe"
)
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [错误] 未找到 Python，请先安装 Python 3。
    pause
    exit /b 1
)

%PY% server.py

pause
