@echo off
chcp 65001 >nul
cd /d %~dp0wa-bridge
title Nova WhatsApp
set restarts=0

where node >nul 2>&1
if errorlevel 1 (
  echo  !!! Node.js ne nayden. Ustanovite Node.js LTS s nodejs.org i zapustite install.bat
  goto done
)
if not exist node_modules (
  echo Ustanavlivayu biblioteki WhatsApp-mosta...
  call npm install --omit=dev
)

:run
echo === Nova WhatsApp (chaty) ===
echo QR-kod dlya privyazki telefona - v brauzere: Chaty -^> knopka "Nuzhen QR"
echo.
node bridge.mjs
set code=%errorlevel%
echo.
echo === WhatsApp-most ostanovlen (kod %code%) %date% %time% ===
if "%code%"=="0" goto done
set /a restarts+=1
if %restarts% GTR 30 (
  echo  !!! Most padaet snova i snova. Prishlite skrinshot etogo okna.
  goto done
)
echo Perezapusk cherez 15 sekund (popytka %restarts%)... Zakryt okno = ostanovit chaty.
timeout /t 15 /nobreak >nul
goto run

:done
echo.
pause
