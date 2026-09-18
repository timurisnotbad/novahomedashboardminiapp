@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Home - restart
echo ============================================
echo   Nova Home - restart server and bot
echo ============================================
echo.

echo [1/5] Proverka Python...
python --version
if errorlevel 1 (
  echo.
  echo  !!! Python ne nayden v PATH. Ustanovite Python 3.11 s python.org
  echo  !!! i postavte galochku "Add python.exe to PATH".
  pause
  exit /b 1
)

echo [2/5] Ostanavlivayu starye processy Nova...
rem the .bat windows restart python when it exits, so they must go first
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -ne 'powershell.exe' -and ($_.CommandLine -like '*start_bot.bat*' -or $_.CommandLine -like '*start_server.bat*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -ne 'powershell.exe' -and ($_.CommandLine -like '*backend.main:app*' -or $_.CommandLine -like '*bot.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do (
    echo     osvobozhdayu port 8000 (PID %%p)
    taskkill /F /PID %%p >nul 2>&1
)
timeout /t 3 /nobreak >nul

netstat -ano | findstr ":8000 " | findstr LISTENING >nul 2>&1
if %errorlevel%==0 (
    echo.
    echo  !!! Port 8000 vsyo eshchyo zanyat. Zakroyte okna "Nova Backend"
    echo  !!! vruchnuyu ili perezagruzite kompyuter i zapustite snova.
    pause
    exit /b 1
)

echo [3/5] Zapuskayu server i bota...
start "Nova Backend" cmd /k "%~dp0start_server.bat"
start "Nova Bot" cmd /k "%~dp0start_bot.bat"

echo [4/5] Zhdu otveta servera...
timeout /t 8 /nobreak >nul
powershell -NoProfile -Command "try { $h = Invoke-RestMethod http://localhost:8000/api/health -TimeoutSec 5; Write-Host ('     server version: ' + $h.version) } catch { Write-Host '     server ne otvetil - smotrite okno Nova Backend' }"

echo [5/5] Proveryayu, chto bot zhiv...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bot.py*' -and $_.Name -like 'python*' }; if ($p) { Write-Host '     bot rabotaet' } else { Write-Host '     BOT NE ZAPUSTILSYA - smotrite okno Nova Bot i fayl logs\bot-error.log' }"
echo.
echo Gotovo. Otkryty dva okna: Nova Backend i Nova Bot.
echo Esli chto-to ne tak - zapustite diagnose.bat i prishlite skrinshot.
timeout /t 12 >nul
