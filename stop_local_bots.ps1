# Останавливает локальные Python-процессы, запущенные из этой папки:
# bot.py, remote_agent.py, vps_bot.py
# Запускается через stop_local_bots.bat (двойной клик)

$ErrorActionPreference = 'Stop'
$targets = @('bot.py', 'remote_agent.py', 'vps_bot.py')

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
$found = $procs | Where-Object {
    $cl = $_.CommandLine
    if (-not $cl) { return $false }
    foreach ($t in $targets) { if ($cl -like "*$t*") { return $true } }
    return $false
}

if (-not $found) {
    Write-Host '  Запущенных ботов не найдено.' -ForegroundColor Green
    exit 0
}

foreach ($p in $found) {
    Write-Host ('  Закрываю PID {0}: {1}' -f $p.ProcessId, $p.CommandLine) -ForegroundColor Yellow
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    } catch {
        Write-Host ('    Не удалось: {0}' -f $_.Exception.Message) -ForegroundColor Red
    }
}

Start-Sleep -Seconds 1

$still = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {
    $cl = $_.CommandLine
    if (-not $cl) { return $false }
    foreach ($t in $targets) { if ($cl -like "*$t*") { return $true } }
    return $false
}

if ($still) {
    Write-Host '  ВНИМАНИЕ: остались процессы:' -ForegroundColor Red
    $still | ForEach-Object { Write-Host ('    PID {0}' -f $_.ProcessId) -ForegroundColor Red }
} else {
    Write-Host '  Все локальные боты остановлены.' -ForegroundColor Green
}

