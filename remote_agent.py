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

# Логирование настраиваем ДО импорта agent.py — иначе agent.basicConfig
# перехватит все логи в agent.log. force=True перебивает любой предыдущий конфиг.
_LOGS_DIR = ROOT / os.getenv("LOGS_DIR", "logs")
_LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOGS_DIR / "remote_agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,
)
log = logging.getLogger("remote_agent")

from config import CHROME_CDP_URL, DELAY_BETWEEN_JOBS_SEC, GDRIVE_CREDENTIALS_JSON, GDRIVE_FOLDER_ID, get_mode  # noqa: E402
from agent import process_one_file, process_research  # noqa: E402
from ssh_tunnel import SSHTunnel  # noqa: E402
from worker_lease_client import claim_lease  # noqa: E402

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
                try:
                    await client.post(f"{api_url}/api/heartbeat", headers=headers)
                except Exception:
                    pass

                # --- Failover-лиз: если активен другой воркер, в standby ---
                if WORKER_ID:
                    active = await claim_lease(client, api_url, VPS_API_TOKEN,
                                               WORKER_ID, WORKER_PRIORITY)
                    if not active:
                        log.info("standby (worker=%s, активен другой) — жду %d сек.",
                                 WORKER_ID, POLL_INTERVAL)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                # Chrome мёртв — задачу НЕ берём, иначе она сгорит об
                # ECONNREFUSED за 3 быстрые попытки (грабля 2026-06-12).
                # Поднять Chrome может вотчдог (кнопка «Запустить» в Telegram).
                try:
                    await client.get(f"{CHROME_CDP_URL}/json/version", timeout=3)
                except Exception:
                    log.warning("Chrome CDP не отвечает (%s) — задачи не беру, "
                                "жду 30 сек.", CHROME_CDP_URL)
                    await asyncio.sleep(30)
                    continue

                # --- Получаем следующую задачу ---
                r = await client.get(f"{api_url}/api/next-job", headers=headers)
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
                                await asyncio.sleep(15)
                            photo, utp = await process_research(
                                job_brand, job_model, job_specs)
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
                            log.warning("research: попытка %d/3 не удалась: %s", attempt, e)
                    if last_error is not None:
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
                r.raise_for_status()
                suffix = Path(input_filename).suffix or ".jpg"
                tmp_input = ROOT / "input" / f"remote_{job_id}{suffix}"
                tmp_input.write_bytes(r.content)

                output_path: Path | None = None
                MAX_ATTEMPTS = 3
                last_error: Exception | None = None

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
                            )
                            log.info("Обработано → %s", output_path)
                            last_error = None
                            break  # успех


                        except Exception as e:
                            last_error = e
                            log.warning("Попытка %d/%d не удалась: %s", attempt, MAX_ATTEMPTS, e)

                    if last_error is not None:
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
