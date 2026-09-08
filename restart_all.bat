@echo off
cd /d %~dp0
echo ============================================
echo   Nova Home - restart server and bot
echo ============================================
echo.
echo Stopping old Nova processes (only ours, not every python.exe)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*backend.main:app*' -or $_.CommandLine -like '*bot.py*') -and $_.Name -ne 'powershell.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Waiting 3 seconds...
timeout /t 3 /nobreak >nul
echo Starting...
start "Nova Backend" cmd /k python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
start "Nova Bot" cmd /k python bot.py
echo.
echo Done. Two windows opened: Nova Backend and Nova Bot.
echo App check: http://localhost:8000
timeout /t 8 >nul
