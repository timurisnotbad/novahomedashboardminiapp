@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Bot
echo === Nova Bot ===
echo.
python bot.py
echo.
echo === Bot ostanovlen. Oshibka vyshe i v logs\bot-error.log ===
pause
