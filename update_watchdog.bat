@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===============================================
echo   Обновление фотоген-агента (watchdog)
echo ===============================================
echo.
echo [1/2] Скачиваю свежий код с GitHub...
git pull
echo.
echo [2/2] Перезапускаю watchdog (новый код, без мигающих окон)...
REM Убиваем ТОЛЬКО watchdog (python/pythonw с agent_watchdog в команде) — остальное не трогаем.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'python.exe','pythonw.exe' -and $_.CommandLine -match 'agent_watchdog' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
REM Запускаем задачу планировщика заново — она поднимет watchdog с новым кодом.
schtasks /run /tn RitualB2B_Watchdog >nul 2>&1
echo.
echo Готово! Окна PowerShell больше не будут мигать.
echo Можно закрыть это окно.
pause
