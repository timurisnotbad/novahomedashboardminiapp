@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Home - ustanovka
echo ============================================
echo   Nova Home - ustanovka / obnovlenie
echo   (dashbord + bot + CRM + chaty)
echo ============================================
echo.

echo [1/5] Python...
python --version
if errorlevel 1 (
  echo.
  echo  !!! Python ne nayden. Ustanovite Python 3.11+ s python.org
  echo  !!! i postavte galochku "Add python.exe to PATH". Zatem zapustite install.bat snova.
  pause
  exit /b 1
)

echo.
echo [2/5] Biblioteki Python...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r backend\requirements.txt
if errorlevel 1 (
  echo  !!! Oshibka ustanovki bibliotek - prishlite skrinshot etogo okna
  pause
  exit /b 1
)

echo.
echo [3/5] Fayl nastroek .env...
python setup_env.py

echo.
echo [4/5] Node.js dlya WhatsApp po QR (neobyazatelno, esli est Wazzup)...
where node >nul 2>&1
if errorlevel 1 (
  echo   Node.js ne nayden - WhatsApp po QR-kodu rabotat ne budet.
  echo   Eto ne strashno, esli WhatsApp podklyuchen cherez Wazzup ^(WAZZUP_API_KEY v .env^).
  echo   Nuzhen QR - ustanovite Node.js LTS s nodejs.org i zapustite install.bat eshchyo raz.
) else (
  pushd wa-bridge
  call npm install --omit=dev
  popd
)

echo.
echo [5/5] Proverka...
python diagnose.py

echo.
echo ============================================
echo   Ustanovka zavershena.
echo   1. Otkroyte .env (Bloknot) i vpishite tokeny: BOT_TOKEN, RC_TOKEN, WAZZUP_API_KEY...
echo   2. Zapustite restart_all.bat - podnimutsya server, bot i chaty.
echo   3. CRM: WEBAPP_URL/crm/  (pervyy vhod sozdast administratora)
echo ============================================
echo.
set /p RUN="Zapustit sistemu seychas? (y/n): "
if /i "%RUN%"=="y" start "" "%~dp0restart_all.bat"
pause
