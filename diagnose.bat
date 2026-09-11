@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Home - samoproverka
python diagnose.py
if errorlevel 1 (
  echo.
  echo Python ne zapustilsya. Ustanovite Python 3.11 s python.org
  echo i pri ustanovke postavte galochku "Add python.exe to PATH".
  pause
)
