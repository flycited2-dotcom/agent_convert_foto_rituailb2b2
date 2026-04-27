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
import select
import socket
import sys
import threading
from pathlib import Path

import httpx
import paramiko
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from config import DELAY_BETWEEN_JOBS_SEC, LOGS_DIR
from agent import process_one_file

# --- SSH / API config (из .env) ---
VPS_SSH_HOST  = os.getenv("VPS_SSH_HOST", "186.246.44.204")
VPS_SSH_USER  = os.getenv("VPS_SSH_USER", "root")
VPS_SSH_PASS  = os.getenv("VPS_SSH_PASS", "")
VPS_API_PORT  = int(os.getenv("VPS_API_PORT", "8765"))
VPS_API_TOKEN = os.getenv("VPS_API_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "10"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "remote_agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("remote_agent")


# ---------------------------------------------------------------------------
# Простой SSH-туннель через paramiko (без sshtunnel)
# ---------------------------------------------------------------------------

def _forward_handler(local_sock: socket.socket, transport: paramiko.Transport,
                     remote_host: str, remote_port: int) -> None:
    try:
        chan = transport.open_channel(
            "direct-tcpip", (remote_host, remote_port), local_sock.getpeername()
        )
    except Exception as e:
        log.debug("Не удалось открыть канал: %s", e)
        local_sock.close()
        return

    try:
        while True:
            r, _, _ = select.select([local_sock, chan], [], [], 2)
            if local_sock in r:
                data = local_sock.recv(4096)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(4096)
                if not data:
                    break
                local_sock.sendall(data)
    except Exception:
        pass
    finally:
        local_sock.close()
        chan.close()


class SSHTunnel:
    """Локальный порт → SSH → remote_host:remote_port."""

    def __init__(self, ssh_host: str, ssh_user: str, ssh_pass: str,
                 remote_host: str, remote_port: int) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(ssh_host, username=ssh_user, password=ssh_pass,
                              timeout=15, banner_timeout=30)
        transport = self._client.get_transport()
        transport.set_keepalive(30)

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(10)
        self.local_port: int = self._server.getsockname()[1]

        self._transport = transport
        self._remote_host = remote_host
        self._remote_port = remote_port
        self._active = True

        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()

    def _accept_loop(self) -> None:
        while self._active:
            try:
                self._server.settimeout(1)
                try:
                    sock, _ = self._server.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=_forward_handler,
                    args=(sock, self._transport, self._remote_host, self._remote_port),
                    daemon=True,
                ).start()
            except Exception:
                break

    def close(self) -> None:
        self._active = False
        try:
            self._server.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "SSHTunnel":
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Основной цикл агента
# ---------------------------------------------------------------------------

async def agent_loop(api_url: str) -> None:
    headers = {"x-agent-token": VPS_API_TOKEN}
    log.info("Агент запущен. API: %s  Опрос каждые %d сек.", api_url, POLL_INTERVAL)

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                # --- Получаем следующую задачу ---
                r = await client.get(f"{api_url}/api/next-job", headers=headers)
                if r.status_code == 204:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                r.raise_for_status()
                job = r.json()
                job_id, input_filename = job["id"], job["input_filename"]
                log.info("Задача %d: %s", job_id, input_filename)

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
                            output_path = await process_one_file(tmp_input)
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
                        # --- Загружаем результат на VPS ---
                        async with httpx.AsyncClient(timeout=120) as up:
                            with open(output_path, "rb") as f:
                                r = await up.post(
                                    f"{api_url}/api/complete/{job_id}",
                                    headers=headers,
                                    files={"result": ("result.png", f, "image/png")},
                                )
                        r.raise_for_status()
                        log.info("Загружено на VPS: %s", r.json())

                finally:
                    tmp_input.unlink(missing_ok=True)
                    if output_path and output_path.exists():
                        output_path.unlink(missing_ok=True)

                await asyncio.sleep(DELAY_BETWEEN_JOBS_SEC)

            except httpx.HTTPError as e:
                log.error("Сеть: %s — жду 30 сек.", e)
                await asyncio.sleep(30)
            except Exception as e:
                log.exception("Неожиданная ошибка: %s — жду 30 сек.", e)
                await asyncio.sleep(30)


def main() -> None:
    if not VPS_API_TOKEN:
        log.error("VPS_API_TOKEN не задан в .env — выход.")
        sys.exit(1)

    log.info("Открываю SSH-туннель → %s:%d…", VPS_SSH_HOST, VPS_API_PORT)
    with SSHTunnel(VPS_SSH_HOST, VPS_SSH_USER, VPS_SSH_PASS,
                   "127.0.0.1", VPS_API_PORT) as tunnel:
        api_url = f"http://127.0.0.1:{tunnel.local_port}"
        log.info("Туннель активен: localhost:%d → VPS:%d", tunnel.local_port, VPS_API_PORT)
        asyncio.run(agent_loop(api_url))


if __name__ == "__main__":
    main()
