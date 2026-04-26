@echo off
REM Запуск отдельного Chrome для агента (отдельный профиль, режим CDP)
REM Этот Chrome НЕ мешает твоему обычному Chrome

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set PROFILE_DIR=%~dp0chrome_profile
set DEBUG_PORT=9333

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
