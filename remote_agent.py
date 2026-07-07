"""Агент на локальном ПК: поллит VPS за задачами, обрабатывает через ChatGPT,
отдаёт результат на VPS. Бот при этом живёт на VPS и отвечает в Telegram.

Подключается к VPS через SSH-туннель (paramiko) — никаких открытых портов не нужно.

Запуск: python remote_agent.py
Нужен запущенный Chrome (start_chrome.bat) и .env с VPS_SSH_*/VPS_API_TOKEN.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from datetime import datetime, timedelta  # noqa: E402

# Дорожка (lane, Phase 3 мульти-аккаунта): LANE_ID из CLI/env → свой CDP-порт,
# per-mode project_url и отдельный лог. Без LANE_ID — поведение как раньше.
# argv учитываем ТОЛЬКО при прямом запуске remote_agent.py: при импорте из
# pytest argv[1] — путь теста, он ломал имя лог-файла (грабля 2026-07-07).
_argv_lane = (sys.argv[1] if len(sys.argv) > 1
              and Path(sys.argv[0]).name == "remote_agent.py" else "")
LANE_ID = (_argv_lane or os.getenv("LANE_ID", "")).strip()

# Логирование настраиваем ДО импорта agent.py — иначе agent.basicConfig
# перехватит все логи в agent.log. force=True перебивает любой предыдущий конфиг.
_LOGS_DIR = ROOT / os.getenv("LOGS_DIR", "logs")
_LOGS_DIR.mkdir(parents=True, exist_ok=True)
_LOG_NAME = f"remote_agent_{LANE_ID}.log" if LANE_ID else "remote_agent.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOGS_DIR / _LOG_NAME, encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,
)
log = logging.getLogger("remote_agent")

from config import CHROME_CDP_URL, DELAY_BETWEEN_JOBS_SEC, GDRIVE_CREDENTIALS_JSON, GDRIVE_FOLDER_ID, get_lane, get_mode  # noqa: E402

LANE = get_lane(LANE_ID) if LANE_ID else None
if LANE_ID and LANE is None:
    raise SystemExit(f"LANE_ID={LANE_ID!r} не найден в lanes.json — проверь id дорожки")
# CDP своей дорожки; без дорожки — модульный (одноканальный режим, как раньше)
CDP_URL = LANE.cdp_url if LANE else CHROME_CDP_URL
def agent_caps() -> str:
    """Возможности агента для /api/next-job. research заявляем ТОЛЬКО если режим
    реально выполним: URL — модульный (RESEARCH_PROJECT_URL) ИЛИ override
    дорожки (project_urls.research в lanes.json) + промпт. Десктоп без env брал
    research и жёг их (job 731); а caps без учёта override давал дедлок —
    desktop-a2 МОГ research через RESEARCH_PROJECT_URL_ACC2, но молчал,
    ноут же в standby по аренде (2026-07-07)."""
    cfg = get_mode("research")
    url = ((LANE.project_url_for("research") if LANE else "")
           or cfg.project_url)
    ok = cfg.enabled and bool(url) and bool((cfg.prompt or "").strip())
    return "research" if ok else ""


from agent import UploadLimitError, process_one_file, process_research  # noqa: E402
from ssh_tunnel import SSHTunnel  # noqa: E402
from worker_lease_client import claim_lease  # noqa: E402
import upload_cooldown  # noqa: E402

# Кулдаун при «Максимальное количество загрузок» — persistентный файл (переживает
# рестарт процесса/ПК), см. upload_cooldown.py. Retry-шторм 2026-07-03 не давал
# квоте ChatGPT восстановиться весь день — с кулдауном агент вместо этого замолкает.
COOLDOWN_PATH = _LOGS_DIR / "upload_cooldown_until.txt"
COOLDOWN_MINUTES = int(os.getenv("UPLOAD_COOLDOWN_MINUTES", "45"))

# --- SSH / API config (из .env) ---
VPS_SSH_HOST  = os.getenv("VPS_SSH_HOST", "186.246.44.204")
VPS_SSH_USER  = os.getenv("VPS_SSH_USER", "root")
VPS_SSH_PASS  = os.getenv("VPS_SSH_PASS", "")
VPS_SSH_KEY   = os.getenv("VPS_SSH_KEY", "")
VPS_API_PORT  = int(os.getenv("VPS_API_PORT", "8765"))
VPS_API_TOKEN = os.getenv("VPS_API_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "10"))
WORKER_ID       = os.getenv("WORKER_ID", "").strip()
WORKER_PRIORITY = int(os.getenv("WORKER_PRIORITY", "100"))

# Аренда failover-лиза (Phase 6, фикс 2026-07-06): claim'ер — уникальное имя
# (id дорожки; без дорожки — WORKER_ID), группа аренды — АККАУНТ дорожки:
# дорожки одного аккаунта на разных машинах не молотят параллельно, разных —
# молотят. Без дорожки account пуст → общая legacy-группа (failover как раньше).
# (Блок обязан жить НИЖЕ WORKER_ID: раньше он стоял до его определения и падал
# NameError при запуске без дорожки — latent, вскрыт тестом 2026-07-07.)
LEASE_ID = (LANE.id if LANE else "") or WORKER_ID
LEASE_ACCOUNT = (LANE.account if LANE else "")


# ---------------------------------------------------------------------------
# Самолечение «зомби-Chrome» (3 случая 06-07.07.2026): /json/version отвечает
# (вотчдог считает живым), но connect_over_cdp виснет 180с — задачи горели
# пачками, Chrome пересоздавали руками. Агент — единственный, кто ВИДИТ этот
# симптом, поэтому лечит сам: kill своего Chrome по порту → start_chrome.bat
# со своими портом/профилем → дождаться CDP. Задача при этом уходит в requeue.
# ---------------------------------------------------------------------------
_CDP_HEAL_COOLDOWN_SEC = int(os.getenv("CDP_HEAL_COOLDOWN_SEC", "600"))
_last_chrome_heal: datetime | None = None


def _is_cdp_attach_timeout(e: Exception) -> bool:
    """Зомби-сигнатура: таймаут именно на connect_over_cdp (не селекторы и пр.)."""
    s = str(e)
    return "connect_over_cdp" in s and "Timeout" in s


def _kill_chrome_by_port() -> None:
    """Убить ТОЛЬКО Chrome своего CDP-порта (личный Chrome порт не содержит)."""
    import subprocess
    port = CDP_URL.rsplit(":", 1)[-1]
    cond = ("$_.Name -eq 'chrome.exe' -and "
            f"$_.CommandLine -match 'remote-debugging-port={port}'")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process | Where-Object {{{cond}}} "
         "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)


def _start_chrome() -> None:
    """Поднять свой Chrome (порт/профиль дорожки; без дорожки — дефолты bat)."""
    import subprocess
    cmd = ["cmd", "/c", str(ROOT / "start_chrome.bat")]
    if LANE:
        cmd += [str(LANE.cdp_port), LANE.profile_dir]
    subprocess.Popen(cmd, cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)


def _wait_cdp(timeout: int = 30) -> bool:
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        try:
            httpx.get(f"{CDP_URL}/json/version", timeout=3, trust_env=False)
            return True
        except Exception:
            _t.sleep(1)
    return False


def heal_chrome() -> bool:
    """Пересоздать свой Chrome. False — лечили недавно (кулдаун защищает от
    вечного kill-цикла, если причина глубже) — тогда задача фейлится штатно."""
    global _last_chrome_heal
    now = datetime.now()
    if (_last_chrome_heal is not None
            and (now - _last_chrome_heal).total_seconds() < _CDP_HEAL_COOLDOWN_SEC):
        log.warning("Зомби-Chrome: лечили < %d сек назад — пропускаю (кулдаун).",
                    _CDP_HEAL_COOLDOWN_SEC)
        return False
    _last_chrome_heal = now
    log.warning("Зомби-Chrome (attach-timeout при живом HTTP) — пересоздаю "
                "Chrome %s…", CDP_URL)
    _kill_chrome_by_port()
    _start_chrome()
    ok = _wait_cdp()
    log.warning("Chrome пересоздан: CDP %s", "жив" if ok else "НЕ поднялся за 30с")
    return True


# ---------------------------------------------------------------------------
# Основной цикл агента
# ---------------------------------------------------------------------------

# Результаты, которые не удалось выгрузить на VPS из-за обрыва туннеля:
# (job_id, путь к файлу). Досылаются в начале каждого витка цикла —
# модульный список переживает пересоздание туннеля в main().
PENDING_UPLOADS: list[tuple[int, Path]] = []


async def _upload_result(api_url: str, job_id: int, output_path: Path) -> None:
    """POST результата на VPS. Передаём ИМЕННО локальное имя файла
    (с brand/model) — VPS сохранит под ним же."""
    headers = {"x-agent-token": VPS_API_TOKEN}
    async with httpx.AsyncClient(timeout=120, trust_env=False) as up:
        with open(output_path, "rb") as f:
            r = await up.post(
                f"{api_url}/api/complete/{job_id}",
                headers=headers,
                files={"result": (output_path.name, f, "image/png")},
            )
    r.raise_for_status()
    log.info("Загружено на VPS: %s", r.json())


async def agent_loop(api_url: str) -> None:
    headers = {"x-agent-token": VPS_API_TOKEN}
    log.info("Агент запущен. API: %s  Опрос каждые %d сек.", api_url, POLL_INTERVAL)

    net_errors = 0  # подряд идущих сетевых ошибок (2 → пересоздаём туннель)
    _last_cooldown_log: datetime | None = None  # троттлинг: не спамить лог каждые POLL_INTERVAL

    # trust_env=False отключает системный прокси Windows (иначе 503 через Clash/v2ray)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        while True:
            try:
                # --- Досылаем результаты, не доставленные из-за обрыва туннеля ---
                while PENDING_UPLOADS:
                    p_job_id, p_path = PENDING_UPLOADS[0]
                    if not p_path.exists():
                        PENDING_UPLOADS.pop(0)
                        continue
                    try:
                        await _upload_result(api_url, p_job_id, p_path)
                        p_path.unlink(missing_ok=True)
                        PENDING_UPLOADS.pop(0)
                    except httpx.HTTPStatusError as e:
                        log.error("Досыл задачи %d отклонён VPS (%s) — пропускаю.", p_job_id, e)
                        PENDING_UPLOADS.pop(0)
                    # сетевые ошибки уходят в общий обработчик ниже — файл остаётся в списке

                # Heartbeat — VPS фиксирует время последнего контакта агента
                # (+ per-lane строка, если дорожка задана)
                try:
                    await client.post(f"{api_url}/api/heartbeat", headers=headers,
                                      params={"lane": LANE_ID} if LANE_ID else None)
                except Exception:
                    pass

                # --- Кулдаун после «Максимальное количество загрузок» ---
                now = datetime.now()
                if upload_cooldown.in_cooldown(COOLDOWN_PATH, now):
                    if _last_cooldown_log is None or (now - _last_cooldown_log) > timedelta(minutes=5):
                        until = upload_cooldown.read_cooldown(COOLDOWN_PATH)
                        log.info("В кулдауне из-за лимита загрузок ChatGPT до %s — жду.", until)
                        _last_cooldown_log = now
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # --- Failover-лиз (по аккаунту): если этот же аккаунт активен
                # на другой машине — в standby (у разных аккаунтов ключи разные,
                # они работают параллельно) ---
                if LEASE_ID:
                    active = await claim_lease(client, api_url, VPS_API_TOKEN,
                                               LEASE_ID, WORKER_PRIORITY,
                                               account=LEASE_ACCOUNT)
                    if not active:
                        log.info("standby (lease=%s/%s, активен другой) — жду %d сек.",
                                 LEASE_ID, LEASE_ACCOUNT or "-", POLL_INTERVAL)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                # Chrome мёртв — задачу НЕ берём, иначе она сгорит об
                # ECONNREFUSED за 3 быстрые попытки (грабля 2026-06-12).
                # Поднять Chrome может вотчдог (кнопка «Запустить» в Telegram).
                try:
                    await client.get(f"{CDP_URL}/json/version", timeout=3)
                except Exception:
                    log.warning("Chrome CDP не отвечает (%s) — задачи не беру, "
                                "жду 30 сек.", CDP_URL)
                    await asyncio.sleep(30)
                    continue

                # --- Получаем следующую задачу (caps — что агент реально умеет;
                # lane уходит в claimed_by; modes — allowlist режимов дорожки:
                # чужой mode открыл бы не тот проект/аккаунт) ---
                _params = {"caps": agent_caps(), "lane": LANE_ID or WORKER_ID}
                if LANE and LANE.modes:
                    _params["modes"] = ",".join(LANE.modes)
                r = await client.get(f"{api_url}/api/next-job", headers=headers,
                                     params=_params)
                net_errors = 0  # связь жива
                if r.status_code == 204:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                r.raise_for_status()
                job = r.json()
                job_id, input_filename = job["id"], job["input_filename"]
                job_mode  = job.get("mode") or "ritual"
                job_specs = job.get("specs") or None
                job_brand = job.get("brand") or None
                job_model = job.get("model") or None
                log.info(
                    "Задача %d (mode=%s, brand=%s, model=%s, specs=%d симв.): %s",
                    job_id, job_mode, job_brand or "-", job_model or "-",
                    len(job_specs or ""), input_filename,
                )

                if job_mode == "research":
                    # research: входного фото НЕТ (input_filename='') — это не битая
                    # задача. ChatGPT ищет УТП + генерит изображение по наименованию.
                    last_error = None
                    for attempt in range(1, 4):
                        try:
                            if attempt > 1:
                                log.warning("research: попытка %d/3 для задачи %d…",
                                            attempt, job_id)
                                # пауза побольше: не долбить ChatGPT (жалоба на
                                # «слишком много запросов» 2026-07-02)
                                await asyncio.sleep(60)
                            photo, utp = await process_research(
                                job_brand, job_model, job_specs,
                                cdp_url=CDP_URL,
                                project_url=(LANE.project_url_for("research")
                                             if LANE else None) or None)
                            files = None
                            if photo and photo.exists():
                                files = {"photo": (photo.name, photo.read_bytes(),
                                                   "image/png")}
                            r = await client.post(
                                f"{api_url}/api/complete-research/{job_id}",
                                headers=headers, data={"utp": utp}, files=files,
                                timeout=120)
                            r.raise_for_status()
                            if photo:
                                photo.unlink(missing_ok=True)
                            last_error = None
                            break
                        except Exception as e:
                            last_error = e
                            if _is_cdp_attach_timeout(e):
                                # зомби-Chrome: лечим и возвращаем задачу в пул
                                # (попытки не жжём); кулдаун → штатный fail ниже
                                if heal_chrome():
                                    last_error = "requeue"
                                    break
                                break
                            log.warning("research: попытка %d/3 не удалась: %s", attempt, e)
                    if last_error == "requeue":
                        try:
                            await client.post(f"{api_url}/api/requeue/{job_id}",
                                              headers=headers)
                            log.info("research-задача %d возвращена в очередь "
                                     "(зомби-Chrome вылечен).", job_id)
                        except Exception:
                            pass
                    elif last_error is not None:
                        log.error("research-задача %d провалилась: %s", job_id, last_error)
                        try:
                            await client.post(f"{api_url}/api/fail/{job_id}",
                                              headers=headers,
                                              data={"error": str(last_error)})
                        except Exception:
                            pass
                    await asyncio.sleep(DELAY_BETWEEN_JOBS_SEC)
                    continue

                if not input_filename:
                    # Битая задача (например, после повреждения queue.db) —
                    # помечаем failed, иначе агент крашится на ней в цикле.
                    log.error("Задача %d без input_filename — помечаю failed.", job_id)
                    await client.post(
                        f"{api_url}/api/fail/{job_id}",
                        headers=headers,
                        data={"error": "input_filename отсутствует (битая задача)"},
                    )
                    continue

                # --- Скачиваем входной файл ---
                r = await client.get(f"{api_url}/api/input/{job_id}", headers=headers)
                if r.status_code == 404:
                    # Входной файл пропал из input/ (блуждающая грабля VPS) — задача
                    # битая, ретраи бессмысленны: это не сеть. Без fail она вечно
                    # циклится claim → 404 → аренда вернёт в pending → снова claim
                    # («зависание и тишина», job 699 2026-07-07).
                    log.error("Задача %d: входной файл пропал (404) — помечаю failed.", job_id)
                    await client.post(
                        f"{api_url}/api/fail/{job_id}", headers=headers,
                        data={"error": "входной файл пропал из input/ (404)"})
                    continue
                r.raise_for_status()
                suffix = Path(input_filename).suffix or ".jpg"
                tmp_input = ROOT / "input" / f"remote_{job_id}{suffix}"
                tmp_input.write_bytes(r.content)

                output_path: Path | None = None
                MAX_ATTEMPTS = 3
                last_error: Exception | None = None
                requeue_job = False       # лимит ChatGPT: вернуть задачу в pending

                try:
                    for attempt in range(1, MAX_ATTEMPTS + 1):
                        try:
                            if attempt > 1:
                                log.warning("Попытка %d/%d для задачи %d…", attempt, MAX_ATTEMPTS, job_id)
                                await asyncio.sleep(15)

                            # --- Обрабатываем через ChatGPT ---
                            output_path = await process_one_file(
                                tmp_input, mode=job_mode, specs=job_specs,
                                brand=job_brand, model=job_model,
                                cdp_url=CDP_URL,
                                project_url=(LANE.project_url_for(job_mode)
                                             if LANE else None) or None,
                            )
                            log.info("Обработано → %s", output_path)
                            last_error = None
                            break  # успех

                        except UploadLimitError as e:
                            # Ретраить бессмысленно и вредно: квота меньше всего
                            # восстановится, если долбить её ретраями (2026-07-03).
                            # Кулдаун + мягкий возврат задачи в очередь (Phase 4):
                            # не failed — попытка конвейера не сжигается, задачу
                            # доберёт другая дорожка или я после кулдауна.
                            last_error = e
                            requeue_job = True
                            until = upload_cooldown.start_cooldown(
                                COOLDOWN_PATH, datetime.now(), COOLDOWN_MINUTES)
                            log.error(
                                "Задача %d: лимит загрузок ChatGPT исчерпан (%s) — "
                                "кулдаун до %s, задачу возвращаю в очередь.",
                                job_id, e, until)
                            break

                        except Exception as e:
                            last_error = e
                            if _is_cdp_attach_timeout(e):
                                # зомби-Chrome: лечим, задачу — мягко в очередь
                                # (попытки не жжём); кулдаун → штатный fail
                                if heal_chrome():
                                    requeue_job = True
                                break
                            log.warning("Попытка %d/%d не удалась: %s", attempt, MAX_ATTEMPTS, e)

                    if requeue_job:
                        # Лимит ChatGPT: мягкий возврат (pending, claim снят).
                        # Если requeue не прошёл (сеть/старый API) — фолбэк в fail,
                        # чтобы задача не зависла в processing навсегда.
                        try:
                            r = await client.post(f"{api_url}/api/requeue/{job_id}",
                                                  headers=headers)
                            r.raise_for_status()
                            log.info("Задача %d возвращена в очередь (pending).", job_id)
                        except Exception as re_err:
                            log.error("requeue задачи %d не прошёл (%s) — помечаю failed.",
                                      job_id, re_err)
                            try:
                                await client.post(f"{api_url}/api/fail/{job_id}",
                                                  headers=headers,
                                                  data={"error": str(last_error)})
                            except Exception:
                                pass
                    elif last_error is not None:
                        # Все попытки провалились — сообщаем VPS
                        log.error("Задача %d провалилась после %d попыток: %s", job_id, MAX_ATTEMPTS, last_error)
                        try:
                            await client.post(
                                f"{api_url}/api/fail/{job_id}",
                                headers=headers,
                                data={"error": str(last_error)},
                            )
                        except Exception:
                            pass
                    else:
                        # --- Загружаем на Google Drive (если настроено) ---
                        # Папка по режиму, fallback на общую GDRIVE_FOLDER_ID.
                        mode_cfg = get_mode(job_mode)
                        target_folder = mode_cfg.gdrive_folder_id or GDRIVE_FOLDER_ID
                        if GDRIVE_CREDENTIALS_JSON and target_folder:
                            try:
                                from gdrive import upload_file as gdrive_upload
                                link = await asyncio.get_event_loop().run_in_executor(
                                    None, gdrive_upload, output_path,
                                    target_folder, GDRIVE_CREDENTIALS_JSON,
                                )
                                log.info("Google Drive (%s): %s", job_mode, link)
                            except Exception as gde:
                                log.warning("Google Drive upload failed: %s", gde)

                        # --- Загружаем результат на VPS ---
                        try:
                            await _upload_result(api_url, job_id, output_path)
                        except httpx.HTTPError:
                            # Туннель/сеть упали — результат НЕ теряем:
                            # дошлём после переподключения туннеля.
                            PENDING_UPLOADS.append((job_id, output_path))
                            log.warning(
                                "Выгрузка задачи %d не удалась — результат сохранён, "
                                "дошлю после переподключения.", job_id,
                            )
                            raise

                finally:
                    tmp_input.unlink(missing_ok=True)
                    if (output_path and output_path.exists()
                            and all(p != output_path for _, p in PENDING_UPLOADS)):
                        output_path.unlink(missing_ok=True)

                await asyncio.sleep(DELAY_BETWEEN_JOBS_SEC)

            except httpx.HTTPError as e:
                net_errors += 1
                if net_errors >= 2:
                    log.error("Сеть: %s — %d ошибки подряд, пересоздаю SSH-туннель.",
                              e, net_errors)
                    raise  # main() закроет туннель и откроет новый
                log.error("Сеть: %s — жду 30 сек.", e)
                await asyncio.sleep(30)
            except Exception as e:
                log.exception("Неожиданная ошибка: %s — жду 30 сек.", e)
                await asyncio.sleep(30)


def main() -> None:
    if not VPS_API_TOKEN:
        log.error("VPS_API_TOKEN не задан в .env — выход.")
        sys.exit(1)

    import time as _time_mod
    while True:
        try:
            log.info("Открываю SSH-туннель → %s:%d…", VPS_SSH_HOST, VPS_API_PORT)
            with SSHTunnel(VPS_SSH_HOST, VPS_SSH_USER, VPS_SSH_PASS,
                           "127.0.0.1", VPS_API_PORT, ssh_key=VPS_SSH_KEY) as tunnel:
                api_url = f"http://127.0.0.1:{tunnel.local_port}"
                log.info("Туннель активен: localhost:%d → VPS:%d", tunnel.local_port, VPS_API_PORT)
                asyncio.run(agent_loop(api_url))
        except KeyboardInterrupt:
            log.info("Агент остановлен (Ctrl+C).")
            break
        except Exception as e:
            log.error("SSH-туннель/агент упал: %s — переподключение через 30 сек.", e)
            _time_mod.sleep(30)


if __name__ == "__main__":
    main()
