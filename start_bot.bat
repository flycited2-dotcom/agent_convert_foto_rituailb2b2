@echo off
REM Запуск Telegram-бота агента
cd /d "%~dp0"
echo ============================================================
echo  Telegram-бот: приём фото и обработка через ChatGPT
echo ============================================================
echo.
echo Перед запуском убедись что:
echo  1) Запущен start_chrome.bat (Chrome с CDP-портом)
echo  2) В этом Chrome ты залогинен в ChatGPT Plus
echo  3) В .env заполнен TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_USER_ID
echo.
python bot.py
pause
