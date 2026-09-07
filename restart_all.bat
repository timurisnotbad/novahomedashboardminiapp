@echo off
cd /d %~dp0
echo ============================================
echo   Nova Home - restart server and bot
echo ============================================
echo.
echo Stopping old processes...
taskkill /F /IM python.exe >nul 2>&1
echo Waiting 5 seconds...
timeout /t 5 /nobreak >nul
echo Starting...
start "Nova Backend" cmd /k python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
start "Nova Bot" cmd /k python bot.py
echo.
echo Done. Two windows opened: Nova Backend and Nova Bot.
echo App check: http://localhost:8000
timeout /t 8 >nul
