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
echo Proveryayu rezultat...
python diagnose.py
pause
