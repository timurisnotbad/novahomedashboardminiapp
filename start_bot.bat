@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Bot
echo === Nova Bot ===
echo.
python bot.py
echo.
echo ============================================================
echo   Bot ostanovlen (kod vyhoda: %errorlevel%)
echo ============================================================
if exist "logs\bot-error.log" (
  echo Poslednie stroki iz logs\bot-error.log:
  echo.
  powershell -NoProfile -Command "Get-Content 'logs\bot-error.log' -Tail 25"
)
echo.
pause
