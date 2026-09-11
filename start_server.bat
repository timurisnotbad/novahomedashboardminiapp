@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Backend
echo === Nova Backend (http://localhost:8000) ===
echo.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
echo.
echo === Server ostanovlen. Oshibka vyshe i v logs\server.log ===
pause
