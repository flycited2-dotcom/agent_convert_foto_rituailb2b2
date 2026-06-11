@echo off
title RitualB2B Watchdog
echo ============================================
echo  RitualB2B Watchdog
echo  Поллит VPS за командой из Telegram:
echo  кнопка "Запустить агента" поднимет Chrome + агента
echo ============================================
echo.
cd /d "%~dp0"
python agent_watchdog.py
pause
