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
from config import Lane, my_lanes  # noqa: E402

VPS_SSH_HOST   = os.getenv("VPS_SSH_HOST", "")
VPS_SSH_USER   = os.getenv("VPS_SSH_USER", "root")
VPS_SSH_PASS   = os.getenv("VPS_SSH_PASS", "")
VPS_SSH_KEY    = os.getenv("VPS_SSH_KEY", "")
VPS_API_PORT   = int(os.getenv("VPS_API_PORT", "8765"))
VPS_API_TOKEN  = os.getenv("VPS_API_TOKEN", "")
CHROME_CDP_URL = os.getenv("CHROME_CDP_URL", "http://127.0.0.1:9333").rstrip("/")
POLL_SEC       = int(os.getenv("WATCHDOG_POLL_SEC", "15"))
WORKER_ID      = os.getenv("WORKER_ID", "").strip()   # адресные флаги: agent_command_<id>
# Стаггер подъёма дорожек: два Chrome разом душат CPU/сеть (роутер владельца
# проседает) — поднимаем по одной с паузой (2026-07-15).
STAGGER_SEC    = int(os.getenv("WATCHDOG_STAGGER_SEC", "90"))
# Троттлинг напоминания «остановлено вручную, а очередь ждёт» (сек)
WAKE_NOTE_EVERY = int(os.getenv("WATCHDOG_WAKE_NOTE_SEC", "7200"))
TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_OWNER       = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

# Дорожки ЭТОЙ машины из lanes.json (Phase 5 мульти-аккаунта): команда из
# Telegram применяется ко ВСЕМ дорожкам машины (у каждой — свой Chrome-порт/
# профиль, свой remote_agent с LANE_ID, свой state-файл). Пусто (нет lanes.json
# или машина не в карте) → legacy-режим: одна безымянная дорожка, как раньше.
LANES: list[Lane] = my_lanes()


def targets() -> list[Lane | None]:
    """К кому применять команды/самовосстановление: дорожки машины или [None]
    (legacy-одиночка без lanes.json)."""
    return list(LANES) or [None]


def cdp_url_for(lane: Lane | None) -> str:
    return lane.cdp_url if lane else CHROME_CDP_URL


def agent_match_pattern(lane: Lane | None) -> str:
    """Regex для поиска процесса remote_agent в cmdline: с дорожкой — только её
    процесс (LANE_ID передаётся аргументом и виден в командной строке)."""
    return f"remote_agent.*{lane.id}" if lane else "remote_agent"


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


def agent_running(lane: Lane | None = None) -> bool:
    """Есть ли python-процесс remote_agent (этой дорожки, если задана)."""
    return _count_procs(agent_match_pattern(lane)) > 0


def kill_agent(lane: Lane | None = None) -> bool:
    """Остановить remote_agent (этой дорожки). True если был найден и убит."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process | Where-Object {{{_PY_PROCS} "
         f"-and $_.CommandLine -match '{agent_match_pattern(lane)}'}} "
         "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,  # не мигать окном PowerShell
    )
    pids = r.stdout.strip()
    who = f"remote_agent[{lane.id}]" if lane else "remote_agent"
    if pids:
        log.info("Остановил %s (PID: %s)", who, pids.replace("\n", ", "))
        return True
    log.info("%s не запущен — останавливать нечего.", who)
    return False


def kill_chrome(lane: Lane | None = None) -> bool:
    """Убить ТОЛЬКО ботовский Chrome дорожки (по её CDP-порту в командной
    строке). Личный Chrome пользователя порт не содержит — его не трогаем."""
    port = cdp_url_for(lane).rsplit(":", 1)[-1]
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
        log.info("Остановил ботовский Chrome :%s (PID: %s)", port, pids.replace("\n", ", "))
        return True
    log.info("Ботовский Chrome :%s не запущен — останавливать нечего.", port)
    return False


# --- Желаемое состояние (переживает перезагрузку ПК; файл на дорожку) ---
def state_file(lane: Lane | None) -> Path:
    name = f"agent_state_{lane.id}.txt" if lane else "agent_state.txt"
    return _LOGS_DIR / name


def set_desired_state(state: str, lane: Lane | None = None) -> None:
    try:
        state_file(lane).write_text(state, encoding="utf-8")
    except Exception as e:
        log.warning("Не записал %s: %s", state_file(lane).name, e)


def desired_state(lane: Lane | None = None) -> str:
    try:
        return state_file(lane).read_text(encoding="utf-8").strip()
    except Exception:
        return "stopped"


def chrome_cdp_alive(lane: Lane | None = None) -> bool:
    try:
        httpx.get(f"{cdp_url_for(lane)}/json/version", timeout=3, trust_env=False)
        return True
    except Exception:
        return False


# ── автопробуждение 'wake' ≠ ручной 'start' (2026-07-15) ────────────────────
# cf-cards бродкастит команду при постановке задач. Раньше это был 'start' —
# он перебивал ручной стоп владельца («жму стоп — вотчдог поднимает»). 'wake'
# поднимает только УПАВШИЕ running-дорожки; остановленные вручную — не трогает,
# владельцу уходит напоминание (не чаще WAKE_NOTE_EVERY).

def plan_wake(entries: list[tuple[str, str, bool]]) -> tuple[list[str], list[str]]:
    """entries: (lane_id, desired_state, healthy) → (кого поднять, кому отказано).
    Чистая функция — тестируется без вотчдога."""
    start, denied = [], []
    for lane_id, desired, healthy in entries:
        if desired == "running":
            if not healthy:
                start.append(lane_id)
        else:
            denied.append(lane_id)
    return start, denied


_WAKE_NOTE_FILE = _LOGS_DIR / "wake_note_ts.txt"


def notify_owner_throttled(text: str) -> None:
    """Сообщение владельцу в Telegram (токен локального бота из .env),
    не чаще WAKE_NOTE_EVERY. Без токена/чата — только лог."""
    try:
        last = float(_WAKE_NOTE_FILE.read_text().strip())
    except Exception:
        last = 0.0
    if time.time() - last < WAKE_NOTE_EVERY:
        return
    log.info("Напоминание владельцу: %s", text)
    if not (TG_TOKEN and TG_OWNER):
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                   data={"chat_id": TG_OWNER, "text": text},
                   timeout=15, trust_env=False)
        _WAKE_NOTE_FILE.write_text(str(time.time()), encoding="utf-8")
    except Exception as e:
        log.warning("Напоминание не отправилось: %s", e)


def handle_start(lane: Lane | None = None) -> None:
    # Сначала Chrome: агент может работать при мёртвом CDP — тогда задачи
    # будут падать, а START "ничего не делал" (грабля 2026-06-12).
    who = f"[{lane.id}] " if lane else ""
    if not chrome_cdp_alive(lane):
        log.info("%sChrome CDP не отвечает — запускаю start_chrome.bat…", who)
        chrome_cmd = ["cmd", "/c", str(ROOT / "start_chrome.bat")]
        if lane:                        # свой порт и профиль дорожки (Phase 2)
            chrome_cmd += [str(lane.cdp_port), lane.profile_dir]
        subprocess.Popen(chrome_cmd, cwd=ROOT,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(30):
            if chrome_cdp_alive(lane):
                break
            time.sleep(1)
        else:
            log.error("%sChrome не поднялся за 30 сек — агент всё равно попробую запустить.", who)

    if agent_running(lane):
        log.info("%sАгент уже работает — повторный запуск не нужен.", who)
        return

    log.info("%sЗапускаю remote_agent.py…", who)
    agent_cmd = [sys.executable, "remote_agent.py"]
    if lane:
        agent_cmd.append(lane.id)       # LANE_ID аргументом (виден в cmdline)
    subprocess.Popen(agent_cmd, cwd=ROOT,
                     creationflags=subprocess.CREATE_NO_WINDOW)  # фон, без окна


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
                            # Команда применяется ко ВСЕМ дорожкам этой машины
                            # (legacy без lanes.json: одна безымянная дорожка).
                            if cmd == "start":
                                log.info("Команда START из Telegram (%d дорожек).",
                                         len(targets()))
                                for i, t in enumerate(targets()):
                                    if i:                       # стаггер: по одной,
                                        time.sleep(STAGGER_SEC)  # не душим CPU/сеть
                                    set_desired_state("running", t)
                                    handle_start(t)
                            elif cmd == "wake":
                                # автопробуждение от cf-cards: поднимает только
                                # УПАВШИЕ running-дорожки; ручной стоп не перебивает
                                entries = [((t.id if t else "legacy"), desired_state(t),
                                            chrome_cdp_alive(t) and agent_running(t))
                                           for t in targets()]
                                start_ids, denied = plan_wake(entries)
                                by_id = {(t.id if t else "legacy"): t for t in targets()}
                                for i, lane_id in enumerate(start_ids):
                                    if i:
                                        time.sleep(STAGGER_SEC)
                                    log.info("WAKE: поднимаю упавшую [%s]", lane_id)
                                    handle_start(by_id[lane_id])
                                if denied and not start_ids:
                                    notify_owner_throttled(
                                        "⏸ Дорожки остановлены вручную, а очередь "
                                        "прислала задачи. Автоподъём не делаю — "
                                        "запусти кнопкой «🚀 Запустить», когда "
                                        "лимит остынет.")
                            elif cmd == "restart":
                                log.info("Команда RESTART из Telegram (%d дорожек).",
                                         len(targets()))
                                for t in targets():
                                    set_desired_state("running", t)
                                    kill_agent(t)
                                    kill_chrome(t)
                                if LANES:
                                    kill_agent(None)  # legacy-агент без LANE_ID
                                                      # (запущен до апдейта)
                                time.sleep(3)
                                for i, t in enumerate(targets()):
                                    if i:
                                        time.sleep(STAGGER_SEC)
                                    handle_start(t)
                            elif cmd == "stop":
                                log.info("Команда STOP из Telegram (%d дорожек).",
                                         len(targets()))
                                for t in targets():
                                    set_desired_state("stopped", t)
                                    kill_agent(t)
                                    kill_chrome(t)  # иначе Chrome с ChatGPT висит
                                                    # без дела до следующего "start"
                                if LANES:
                                    kill_agent(None)  # legacy-агент без LANE_ID

                            # Самовосстановление раз в ~минуту: если дорожка
                            # должна работать, а её Chrome/агент умерли — поднимаем.
                            enforce_tick += 1
                            if enforce_tick >= 4 and not cmd:
                                enforce_tick = 0
                                for t in targets():
                                    if desired_state(t) == "running" and (
                                            not chrome_cdp_alive(t) or not agent_running(t)):
                                        log.warning(
                                            "Самовосстановление%s: Chrome или агент "
                                            "умерли — поднимаю.",
                                            f" [{t.id}]" if t else "")
                                        handle_start(t)
                                        break   # одну за проход (стаггер: вторая —
                                                # на следующем тике через ~минуту)
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
