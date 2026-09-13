@echo off
chcp 65001 >nul
title Moontalk 桌面应用
echo ============================================
echo   Moontalk - 启动桌面应用
echo ============================================
echo.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到项目虚拟环境，正在创建...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo 创建 Python 虚拟环境失败，请确认已安装 Python 3.10+。
    pause
    exit /b 1
  )
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
python desktop_app.py
pause
