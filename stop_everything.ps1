# "Красная кнопка": глушит АБСОЛЮТНО всё, что связано с фотоагентом на этой машине.
#
# В отличие от старого stop_local_bots.ps1 (только bot.py/remote_agent.py/vps_bot.py),
# эта версия дополнительно:
#   - убивает agent_watchdog.py — иначе он САМ поднимет remote_agent.py заново в течение
#     минуты (self-healing цикл), и "остановка" окажется фиктивной;
#   - отключает задачу планировщика RitualB2B_Watchdog_Laptop — иначе она перезапустит
#     watchdog при следующем срабатывании (каждые 5 мин или при следующем логоне);
#   - убивает ботовский Chrome (CDP-порт из .env), а не личный Chrome пользователя;
#   - пишет logs/agent_state.txt = stopped — если что-то всё же уцелеет и опросит
#     желаемое состояние, оно увидит "стоп" и не будет самовосстанавливаться.
# Инцидент 2026-07-03 останавливался вручную по этим же шагам — здесь это одна кнопка.

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot

Write-Host '=== 1. Отключаю задачу планировщика (авто-перезапуск watchdog) ===' -ForegroundColor Cyan
$task = Get-ScheduledTask -TaskName 'RitualB2B_Watchdog_Laptop' -ErrorAction SilentlyContinue
if ($task) {
    Disable-ScheduledTask -TaskName 'RitualB2B_Watchdog_Laptop' -ErrorAction SilentlyContinue | Out-Null
    Write-Host '  Задача отключена.' -ForegroundColor Green
} else {
    Write-Host '  Задача не найдена — пропускаю.' -ForegroundColor Yellow
}

Write-Host '=== 2. Останавливаю python-процессы агента ===' -ForegroundColor Cyan
$targets = @('agent_watchdog.py', 'remote_agent.py', 'bot.py', 'vps_bot.py')
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
$found = $procs | Where-Object {
    $cl = $_.CommandLine
    if (-not $cl) { return $false }
    foreach ($t in $targets) { if ($cl -like "*$t*") { return $true } }
    return $false
}
if ($found) {
    foreach ($p in $found) {
        Write-Host ('  Закрываю PID {0}: {1}' -f $p.ProcessId, $p.CommandLine) -ForegroundColor Yellow
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop }
        catch { Write-Host ('    Не удалось: {0}' -f $_.Exception.Message) -ForegroundColor Red }
    }
} else {
    Write-Host '  Python-процессов агента не найдено.' -ForegroundColor Green
}

Write-Host '=== 3. Останавливаю ботовский Chrome (CDP-порт) ===' -ForegroundColor Cyan
$envFile = Join-Path $root '.env'
$cdpPort = '9333'
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^CHROME_CDP_URL=' }
    if ($line -and $line -match ':(\d+)\s*$') { $cdpPort = $Matches[1] }
}
$chromeProcs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {
    $_.CommandLine -match "remote-debugging-port=$cdpPort"
}
if ($chromeProcs) {
    foreach ($p in $chromeProcs) {
        Write-Host ('  Закрываю ботовский Chrome PID {0}' -f $p.ProcessId) -ForegroundColor Yellow
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "  Ботовский Chrome (порт $cdpPort) не запущен." -ForegroundColor Green
}

Write-Host '=== 4. Записываю желаемое состояние: stopped ===' -ForegroundColor Cyan
$logsDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
Set-Content -Path (Join-Path $logsDir 'agent_state.txt') -Value 'stopped' -NoNewline -Encoding utf8
Write-Host '  logs/agent_state.txt = stopped' -ForegroundColor Green

Write-Host ''
Write-Host 'ГОТОВО: всё остановлено. Личный Chrome не тронут.' -ForegroundColor Green
Write-Host 'Чтобы включить обратно — "Запустить все процессы.bat".' -ForegroundColor Green
