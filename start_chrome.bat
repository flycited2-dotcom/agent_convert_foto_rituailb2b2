@echo off
REM Запуск отдельного Chrome для агента (отдельный профиль, режим CDP)
REM Этот Chrome НЕ мешает твоему обычному Chrome
REM
REM Параметры (мульти-аккаунт, Phase 2): %1 = CDP-порт, %2 = имя папки профиля.
REM Без параметров — как раньше (порт 9333, профиль chrome_profile).
REM Вторая дорожка (аккаунт 2): start_chrome.bat 9334 chrome_profile_acc2
REM   → в открывшемся Chrome один раз войти в ChatGPT-аккаунт 2.

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set DEBUG_PORT=%~1
if "%DEBUG_PORT%"=="" set DEBUG_PORT=9333
set PROFILE_NAME=%~2
if "%PROFILE_NAME%"=="" set PROFILE_NAME=chrome_profile
set PROFILE_DIR=%~dp0%PROFILE_NAME%

echo ============================================================
echo  Запуск Chrome для агента обработки фото
echo  Порт отладки: %DEBUG_PORT%
echo  Профиль: %PROFILE_DIR%
echo ============================================================
echo.
echo Если открывается впервые — войди в свой ChatGPT (Plus аккаунт).
echo Сессия сохранится. В следующий раз логиниться не нужно.
echo.

start "" %CHROME% ^
    --remote-debugging-port=%DEBUG_PORT% ^
    --user-data-dir="%PROFILE_DIR%" ^
    --no-first-run ^
    --no-default-browser-check ^
    --disable-features=TranslateUI ^
    https://chatgpt.com/

echo Chrome запущен. Можно закрыть это окно.
timeout /t 3 >nul
