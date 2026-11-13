@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Home - pereezd v oblako
python deploy\move_to_cloud.py
echo.
pause
