"""Вотчдог на локальном ПК: поллит VPS за командами из Telegram.

Команды (флаг agent_command в queue.db на VPS, ставится кнопками бота):
  start   — поднять Chrome (если CDP мёртв) + remote_agent.py (если не запущен)
  restart — убить remote_agent.py И ботовский Chrome, запустить оба заново
            (лекарство от любых зависаний)
  stop    — убить remote_agent.py (и не воскрешать до следующего start)

Желаемое состояние (running/stopped) хранится в logs/agent_state.txt:
start/restart → running, stop → stopped. Раз в минуту вотчдог сверяет
реальность с желаемым и поднимает умершие Chrome/агента (самовосстановление
после перезагрузки ПК, краша агента и т.п.).

Запуск: python agent_watchdog.py  (или start_watchdog.bat;
автозагрузка — задача планировщика RitualB2B_Watchdog)
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

_LOGS_DIR = ROOT / os.getenv("LOGS_DIR", "logs")
_LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOGS_DIR / "watchdog.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("watchdog")

from ssh_tunnel import SSHTunnel  # noqa: E402

VPS_SSH_HOST   = os.getenv("VPS_SSH_HOST", "")
VPS_SSH_USER   = os.getenv("VPS_SSH_USER", "root")
VPS_SSH_PASS   = os.getenv("VPS_SSH_PASS", "")
VPS_SSH_KEY    = os.getenv("VPS_SSH_KEY", "")
VPS_API_PORT   = int(os.getenv("VPS_API_PORT", "8765"))
VPS_API_TOKEN  = os.getenv("VPS_API_TOKEN", "")
CHROME_CDP_URL = os.getenv("CHROME_CDP_URL", "http://127.0.0.1:9333").rstrip("/")
POLL_SEC       = int(os.getenv("WATCHDOG_POLL_SEC", "15"))
WORKER_ID      = os.getenv("WORKER_ID", "").strip()   # адресные флаги: agent_command_<id>


# Фильтр процессов ТОЛЬКО по python/pythonw — иначе match по cmdline ловит
# сам powershell-процесс (его команда содержит искомую строку) → ложные
# срабатывания и риск убить не тот процесс.
_PY_PROCS = "$_.Name -in 'python.exe','pythonw.exe'"


def _count_procs(cmdline_substr: str, exclude_pid: int | None = None) -> int:
    cond = f"{_PY_PROCS} -and $_.CommandLine -match '{cmdline_substr}'"
    if exclude_pid is not None:
        cond += f" -and $_.ProcessId -ne {exclude_pid}"
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-CimInstance Win32_Process | Where-Object {{{cond}}} | Measure-Object).Count"],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,  # не мигать окном PowerShell
    )
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        return 0
    return int(r.stdout.strip())


def another_watchdog_running() -> bool:
    """True если другой экземпляр вотчдога уже работает (кроме текущего PID).
    Защита от дублей: задача планировщика перезапускается каждые 5 мин,
    и если живой вотчдог уже есть — новый сразу выходит."""
    return _count_procs("agent_watchdog", exclude_pid=os.getpid()) > 0


def agent_running() -> bool:
    """Есть ли python-процесс с remote_agent.py в командной строке."""
    return _count_procs("remote_agent") > 0


def kill_agent() -> bool:
    """Остановить remote_agent.py. True если процесс был найден и убит."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process | Where-Object {{{_PY_PROCS} "
         "-and $_.CommandLine -match 'remote_agent'} "
         "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,  # не мигать окном PowerShell
    )
    pids = r.stdout.strip()
    if pids:
        log.info("Остановил remote_agent (PID: %s)", pids.replace("\n", ", "))
        return True
    log.info("remote_agent не запущен — останавливать нечего.")
    return False


def kill_chrome() -> bool:
    """Убить ТОЛЬКО ботовский Chrome (по CDP-порту в командной строке).
    Личный Chrome пользователя порт не содержит — его не трогаем."""
    port = CHROME_CDP_URL.rsplit(":", 1)[-1]
    cond = ("$_.Name -eq 'chrome.exe' -and "
            f"$_.CommandLine -match 'remote-debugging-port={port}'")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process | Where-Object {{{cond}}} "
         "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,  # не мигать окном PowerShell
    )
    pids = r.stdout.strip()
    if pids:
        log.info("Остановил ботовский Chrome (PID: %s)", pids.replace("\n", ", "))
        return True
    log.info("Ботовский Chrome не запущен — останавливать нечего.")
    return False


# --- Желаемое состояние агента (переживает перезагрузку ПК) ---
STATE_FILE = _LOGS_DIR / "agent_state.txt"


def set_desired_state(state: str) -> None:
    try:
        STATE_FILE.write_text(state, encoding="utf-8")
    except Exception as e:
        log.warning("Не записал %s: %s", STATE_FILE.name, e)


def desired_state() -> str:
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "stopped"


def chrome_cdp_alive() -> bool:
    try:
        httpx.get(f"{CHROME_CDP_URL}/json/version", timeout=3, trust_env=False)
        return True
    except Exception:
        return False


def handle_start() -> None:
    # Сначала Chrome: агент может работать при мёртвом CDP — тогда задачи
    # будут падать, а START "ничего не делал" (грабля 2026-06-12).
    if not chrome_cdp_alive():
        log.info("Chrome CDP не отвечает — запускаю start_chrome.bat…")
        subprocess.Popen(
            ["cmd", "/c", str(ROOT / "start_chrome.bat")],
            cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(30):
            if chrome_cdp_alive():
                break
            time.sleep(1)
        else:
            log.error("Chrome не поднялся за 30 сек — агент всё равно попробую запустить.")

    if agent_running():
        log.info("Агент уже работает — повторный запуск не нужен.")
        return

    log.info("Запускаю remote_agent.py…")
    subprocess.Popen(
        [sys.executable, "remote_agent.py"],
        cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW,  # фон, без консольного окна
    )


def main() -> None:
    if not (VPS_SSH_HOST and (VPS_SSH_KEY or VPS_SSH_PASS) and VPS_API_TOKEN):
        log.error("VPS_SSH_HOST/(VPS_SSH_KEY|VPS_SSH_PASS)/VPS_API_TOKEN не заданы в .env — выход.")
        sys.exit(1)

    # Защита от дублей: планировщик перезапускает задачу каждые 5 мин;
    # если вотчдог уже жив — этот экземпляр сразу выходит.
    if another_watchdog_running():
        log.info("Вотчдог уже запущен в другом процессе — выхожу.")
        sys.exit(0)

    headers = {"x-agent-token": VPS_API_TOKEN}
    while True:
        try:
            log.info("Открываю SSH-туннель → %s:%d…", VPS_SSH_HOST, VPS_API_PORT)
            with SSHTunnel(VPS_SSH_HOST, VPS_SSH_USER, VPS_SSH_PASS,
                           "127.0.0.1", VPS_API_PORT, ssh_key=VPS_SSH_KEY) as tunnel:
                api_url = f"http://127.0.0.1:{tunnel.local_port}"
                log.info("Вотчдог на связи: localhost:%d → VPS:%d, опрос каждые %d сек.",
                         tunnel.local_port, VPS_API_PORT, POLL_SEC)
                errors = 0
                enforce_tick = 0
                with httpx.Client(timeout=15, trust_env=False) as client:
                    while True:
                        try:
                            params = {"worker": WORKER_ID} if WORKER_ID else None
                            r = client.get(f"{api_url}/api/agent-command",
                                           headers=headers, params=params)
                            r.raise_for_status()
                            errors = 0
                            cmd = r.json().get("command")
                            if cmd in ("", "none"):   # API отдаёт строку "none", не None!
                                cmd = None            # иначе самовосстановление ниже не работает
                            if cmd == "start":
                                log.info("Команда START из Telegram.")
                                set_desired_state("running")
                                handle_start()
                            elif cmd == "restart":
                                log.info("Команда RESTART из Telegram.")
                                set_desired_state("running")
                                kill_agent()
                                kill_chrome()
                                time.sleep(3)
                                handle_start()
                            elif cmd == "stop":
                                log.info("Команда STOP из Telegram.")
                                set_desired_state("stopped")
                                kill_agent()

                            # Самовосстановление раз в ~минуту: если должны
                            # работать, а Chrome/агент умерли — поднимаем.
                            enforce_tick += 1
                            if enforce_tick >= 4 and not cmd:
                                enforce_tick = 0
                                if desired_state() == "running" and (
                                        not chrome_cdp_alive() or not agent_running()):
                                    log.warning(
                                        "Самовосстановление: Chrome или агент умерли — поднимаю.")
                                    handle_start()
                        except httpx.HTTPError as e:
                            errors += 1
                            log.warning("Сеть: %s (%d подряд)", e, errors)
                            if errors >= 2:
                                raise  # пересоздаём туннель
                        time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            log.info("Вотчдог остановлен (Ctrl+C).")
            break
        except Exception as e:
            log.error("Туннель/сеть: %s — переподключение через 30 сек.", e)
            time.sleep(30)


if __name__ == "__main__":
    main()
