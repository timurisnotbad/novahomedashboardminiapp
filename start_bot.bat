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
set /a restarts+=1
if %restarts% GTR 30 (
  echo.
  echo  !!! Bot padaet snova i snova (30 raz). Zapustite diagnose.bat i prishlite logs\diagnose.txt
  goto done
)
echo.
echo Perezapusk cherez 15 sekund (popytka %restarts%)... Zakryt okno = ostanovit bota.
timeout /t 15 /nobreak >nul
goto run

:done
echo.
pause
