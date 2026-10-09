@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Home - ustanovka bibliotek
echo ============================================
echo   Nova Home - ustanovka / obnovlenie bibliotek
echo ============================================
echo.
python --version
if errorlevel 1 (
  echo.
  echo Python ne nayden. Ustanovite Python 3.11 s python.org,
  echo galochka "Add python.exe to PATH" objazatelna.
  pause
  exit /b 1
)
echo.
echo Ustanavlivayu...
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
echo.
echo Chaty WhatsApp (nuzhen Node.js LTS s nodejs.org)...
where node >nul 2>&1
if errorlevel 1 (
  echo   Node.js ne nayden - chaty WhatsApp ne budut rabotat.
  echo   Ustanovite Node.js LTS s nodejs.org i zapustite install.bat eshchyo raz.
) else (
  pushd wa-bridge
  call npm install --omit=dev
  popd
)
echo.
echo Proveryayu rezultat...
python diagnose.py
pause
