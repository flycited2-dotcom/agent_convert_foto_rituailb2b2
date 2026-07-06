@echo off
REM Chrome dlya agenta (otdelnyj profil, CDP). Ne meshaet lichnomu Chrome.
REM Parametry (multi-account, Phase 2): %1 = CDP-port, %2 = imya papki profilya.
REM Bez parametrov - kak ranshe (9333 / chrome_profile).
REM Vtoroj akkaunt: start_chrome.bat 9334 chrome_profile_acc2 -> vojti v ChatGPT acc2.

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set DEBUG_PORT=%~1
if "%DEBUG_PORT%"=="" set DEBUG_PORT=9333
set PROFILE_NAME=%~2
if "%PROFILE_NAME%"=="" set PROFILE_NAME=chrome_profile
set PROFILE_DIR=%~dp0%PROFILE_NAME%

echo ============================================================
echo  Chrome dlya agenta: port %DEBUG_PORT%, profil %PROFILE_DIR%
echo ============================================================
echo Esli profil novyj - vojdi v ChatGPT, sessiya sohranitsya.

start "" %CHROME% ^
    --remote-debugging-port=%DEBUG_PORT% ^
    --user-data-dir="%PROFILE_DIR%" ^
    --no-first-run ^
    --no-default-browser-check ^
    --disable-features=TranslateUI ^
    https://chatgpt.com/

echo Chrome zapushchen.
timeout /t 3 >nul
