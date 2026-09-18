@echo off
chcp 65001 >nul
cd /d %~dp0
title Nova Home - avtozapusk
echo ============================================
echo   Nova Home - avtozapusk posle perezagruzki
echo ============================================
echo.

echo [1/3] Zapreshchayu kompyuteru zasypat (ot seti)...
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 20
if errorlevel 1 (
  echo     !!! ne udalos: zapustite etot fayl ot imeni administratora ^(pravyy klik^)
) else (
  echo     gotovo: son i gibernatsiya vyklyucheny, ekran gasnet cherez 20 min
)

echo [2/3] Dobavlyayu Nova Home v avtozagruzku...
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if not exist "%STARTUP%" mkdir "%STARTUP%"
> "%STARTUP%\NovaHome.bat" (
  echo @echo off
  echo rem zhdyom set i diski posle vhoda v Windows
  echo timeout /t 40 /nobreak ^>nul
  echo start "" "%~dp0restart_all.bat"
)
echo     sozdan: %STARTUP%\NovaHome.bat

echo [3/3] Chto eshchyo nuzhno sdelat VRUCHNUYU (odin raz):
echo.
echo   a) Avtovhod v Windows, inache posle perezagruzki komp zhdyot parol,
echo      a bot ne zapuskaetsya:
echo        Win+R  ->  netplwiz  ->  snyat galochku "Trebovat vvod imeni i parolya"
echo        ->  OK  ->  vvesti parol dva raza.
echo   b) Parametry -> Center obnovleniya Windows -> Dopolnitelnye parametry ->
echo      "Period aktivnosti" -> vruchnuyu, naprimer 08:00-02:00, chtoby Windows
echo      ne perezagruzhal komp dnyom. Nochnaya perezagruzka teper ne strashna:
echo      bot podnimetsya sam.
echo.
echo Gotovo. Proverit: perezagruzit komp i cherez minutu posle vhoda
echo dolzhny otkrytsya okna "Nova Backend" i "Nova Bot".
echo.
pause
