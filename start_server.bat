@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Backend
set restarts=0

:run
echo === Nova Backend (http://localhost:8000) ===
echo.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
set code=%errorlevel%
echo.
echo === Server ostanovlen (kod %code%) %date% %time%. Oshibka vyshe i v logs\server.log ===
if "%code%"=="0" goto done
set /a restarts+=1
if %restarts% GTR 30 (
  echo  !!! Server padaet snova i snova. Zapustite diagnose.bat i prishlite logs\diagnose.txt
  goto done
)
echo Perezapusk cherez 15 sekund (popytka %restarts%)... Zakryt okno = ostanovit server.
timeout /t 15 /nobreak >nul
goto run

:done
echo.
pause
