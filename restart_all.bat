@echo off
cd /d %~dp0
echo ============================================
echo   Nova Home - restart server and bot
echo ============================================
echo.
echo [1/4] Stopping old Nova processes (by command line)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -ne 'powershell.exe' -and ($_.CommandLine -like '*backend.main:app*' -or $_.CommandLine -like '*bot.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [2/4] Freeing port 8000 (whatever still holds it)...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do (
    echo     killing PID %%p
    taskkill /F /PID %%p >nul 2>&1
)
timeout /t 3 /nobreak >nul

netstat -ano | findstr ":8000 " | findstr LISTENING >nul 2>&1
if %errorlevel%==0 (
    echo.
    echo  !!! Port 8000 is STILL busy. Close every "Nova Backend" window
    echo  !!! manually (or reboot) and run this file again.
    echo.
    pause
    exit /b 1
)

echo [3/4] Starting server and bot...
start "Nova Backend" cmd /k python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
start "Nova Bot" cmd /k python bot.py

echo [4/4] Checking the server answers with the new version...
timeout /t 6 /nobreak >nul
powershell -NoProfile -Command "try { $h = Invoke-RestMethod http://localhost:8000/api/health -TimeoutSec 5; Write-Host ('     server version: ' + $h.version) } catch { Write-Host '     server did not answer yet - look at the Nova Backend window' }"
echo.
echo Done. Two windows opened: Nova Backend and Nova Bot.
echo If either window shows a red error, send its screenshot.
timeout /t 10 >nul
