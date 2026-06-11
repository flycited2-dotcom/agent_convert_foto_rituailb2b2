"""Вотчдог на локальном ПК: поллит VPS за командами из Telegram.

Команды (флаг agent_command в queue.db на VPS, ставится кнопками бота):
  start   — поднять Chrome (если CDP мёртв) + remote_agent.py (если не запущен)
  restart — убить remote_agent.py и запустить заново (если завис)
  stop    — убить remote_agent.py

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
    )
    pids = r.stdout.strip()
    if pids:
        log.info("Остановил remote_agent (PID: %s)", pids.replace("\n", ", "))
        return True
    log.info("remote_agent не запущен — останавливать нечего.")
    return False


def chrome_cdp_alive() -> bool:
    try:
        httpx.get(f"{CHROME_CDP_URL}/json/version", timeout=3, trust_env=False)
        return True
    except Exception:
        return False


def handle_start() -> None:
    if agent_running():
        log.info("Агент уже работает — повторный запуск не нужен.")
        return

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

    log.info("Запускаю remote_agent.py…")
    subprocess.Popen(
        [sys.executable, "remote_agent.py"],
        cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE,
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
                with httpx.Client(timeout=15, trust_env=False) as client:
                    while True:
                        try:
                            r = client.get(f"{api_url}/api/agent-command", headers=headers)
                            r.raise_for_status()
                            errors = 0
                            cmd = r.json().get("command")
                            if cmd == "start":
                                log.info("Команда START из Telegram.")
                                handle_start()
                            elif cmd == "restart":
                                log.info("Команда RESTART из Telegram.")
                                kill_agent()
                                time.sleep(3)
                                handle_start()
                            elif cmd == "stop":
                                log.info("Команда STOP из Telegram.")
                                kill_agent()
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
