@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Home - obnovlenie servera v oblake
python deploy\cloud.py update
echo.
pause
