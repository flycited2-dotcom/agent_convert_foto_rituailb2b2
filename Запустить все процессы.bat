@echo off
chcp 65001 >nul
title ЗАПУСТИТЬ ВСЕ ПРОЦЕССЫ

echo ============================================================
echo   ЗАПУСК ВСЕХ ПРОЦЕССОВ ФОТОАГЕНТА (watchdog поднимет остальное)
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_everything.ps1"

echo.
echo ============================================================
pause
