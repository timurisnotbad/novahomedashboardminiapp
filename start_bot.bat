@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Bot
set restarts=0

:run
echo === Nova Bot ===
echo.
python bot.py
set code=%errorlevel%
echo.
echo ============================================================
echo   Bot ostanovlen (kod vyhoda: %code%)  %date% %time%
echo ============================================================
if "%code%"=="0" goto done
if "%code%"=="2" goto done
if exist "logs\bot-error.log" (
  echo Poslednie stroki iz logs\bot-error.log:
  echo.
  powershell -NoProfile -Command "Get-Content 'logs\bot-error.log' -Tail 15"
)
rem kod 3 = bot sam poprosil perezapusk (zavis opros Telegram): ne schitaem
if "%code%"=="3" goto again
set /a restarts+=1
if %restarts% GTR 30 (
  echo.
  echo  !!! Bot padaet snova i snova. Zhdu 10 minut i probuyu eshchyo: okno NE zakryvat.
  echo  !!! Prichina - v logs\bot-error.log, server prishlyot eyo v Telegram.
  timeout /t 600 /nobreak >nul
  set restarts=0
)
:again
echo.
echo Perezapusk cherez 15 sekund... Zakryt okno = ostanovit bota.
timeout /t 15 /nobreak >nul
goto run

:done
echo.
pause
