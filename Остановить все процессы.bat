@echo off
chcp 65001 >nul
title ОСТАНОВИТЬ ВСЕ ПРОЦЕССЫ

echo ============================================================
echo   ОСТАНОВКА ВСЕХ ПРОЦЕССОВ ФОТОАГЕНТА (watchdog, агент, Chrome)
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_everything.ps1"

echo.
echo ============================================================
pause
