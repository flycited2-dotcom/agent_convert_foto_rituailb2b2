@echo off
chcp 65001 >nul
title Остановка локальных ботов

echo ============================================================
echo   Остановка всех локальных ботов и агентов
echo   (bot.py, remote_agent.py, vps_bot.py)
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_local_bots.ps1"

echo.
echo ============================================================
pause
