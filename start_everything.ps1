# "Зелёная кнопка": включает всё обратно после "Остановить все процессы.bat".
# Включает задачу планировщика, помечает желаемое состояние running и сразу
# запускает watchdog (не ждём до 5 минут следующего тика задачи) — он сам поднимет
# Chrome и remote_agent.py по своей штатной логике самовосстановления.

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot

Write-Host '=== 1. Включаю задачу планировщика ===' -ForegroundColor Cyan
$task = Get-ScheduledTask -TaskName 'RitualB2B_Watchdog_Laptop' -ErrorAction SilentlyContinue
if ($task) {
    Enable-ScheduledTask -TaskName 'RitualB2B_Watchdog_Laptop' -ErrorAction SilentlyContinue | Out-Null
    Write-Host '  Задача включена.' -ForegroundColor Green
} else {
    Write-Host '  Задача не найдена — пропускаю (запущу watchdog напрямую).' -ForegroundColor Yellow
}

Write-Host '=== 2. Записываю желаемое состояние: running ===' -ForegroundColor Cyan
$logsDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
Set-Content -Path (Join-Path $logsDir 'agent_state.txt') -Value 'running' -NoNewline -Encoding utf8
Write-Host '  logs/agent_state.txt = running' -ForegroundColor Green

Write-Host '=== 3. Запускаю watchdog ===' -ForegroundColor Cyan
$already = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {
    $_.CommandLine -match 'agent_watchdog\.py'
}
if ($already) {
    Write-Host '  Watchdog уже работает — он сам поднимет Chrome/агента (самовосстановление).' -ForegroundColor Green
} else {
    Start-Process -FilePath 'pythonw.exe' -ArgumentList 'agent_watchdog.py' -WorkingDirectory $root -WindowStyle Hidden
    Write-Host '  Watchdog запущен.' -ForegroundColor Green
}

Write-Host ''
Write-Host 'ГОТОВО: watchdog поднимет Chrome и агента в течение ~30 сек.' -ForegroundColor Green
Write-Host 'Проверить: logs\watchdog.log и logs\remote_agent.log' -ForegroundColor Green
