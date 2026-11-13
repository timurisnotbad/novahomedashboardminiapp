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
rem lyuboy vyhod - perezapusk: server dolzhen rabotat vsegda (zakryt okno = ostanovit)
set /a restarts+=1
if %restarts% LEQ 30 goto again
echo  !!! Server padaet snova i snova. Zhdu 10 minut i probuyu eshchyo. Prishlite logs\server-error.log
timeout /t 600 /nobreak >nul
set restarts=0

:again
echo Perezapusk cherez 15 sekund... Zakryt okno = ostanovit server.
timeout /t 15 /nobreak >nul
goto run
