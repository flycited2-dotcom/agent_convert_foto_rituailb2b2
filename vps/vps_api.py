"""HTTP API для агента на локальном ПК.

Эндпоинты:
  GET  /api/next-job          → следующая задача или 204
  GET  /api/input/{job_id}    → скачать входное фото
  POST /api/complete/{job_id} → загрузить результат (multipart: result=<file>)
  POST /api/fail/{job_id}     → пометить как ошибку (form: error=<text>)
  POST /api/submit-job        → внешний клиент ставит задачу (multipart: photo +
                                form mode/specs/brand/model/chat_id)

Запуск: uvicorn vps_api:app --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import io

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from config_vps import API_TOKEN, DB_PATH, FAILED_DIR, INPUT_DIR, OUTPUT_DIR, PROCESSED_DIR
from worker_lease import LEASE_TTL_SECONDS, active_worker_id

log = logging.getLogger("vps_api")
app = FastAPI(docs_url=None, redoc_url=None)  # отключаем Swagger UI в prod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _auth(x_agent_token: str = Header(...)) -> None:
    if API_TOKEN and x_agent_token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/next-job")
def next_job(x_agent_token: str = Header(...)):
    _auth(x_agent_token)
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return JSONResponse(status_code=204, content=None)
        conn.execute(
            "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
            (datetime.now().isoformat(), row["id"]),
        )
        conn.commit()
    keys = row.keys()
    mode  = (row["mode"]  if "mode"  in keys else None) or "ritual"
    specs = (row["specs"] if "specs" in keys else None) or ""
    brand = (row["brand"] if "brand" in keys else None) or ""
    model = (row["model"] if "model" in keys else None) or ""
    return {
        "id": row["id"],
        "input_filename": row["input_filename"],
        "mode": mode,
        "specs": specs,
        "brand": brand,
        "model": model,
    }


@app.get("/api/input/{job_id}")
def get_input(job_id: int, x_agent_token: str = Header(...)):
    _auth(x_agent_token)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    path = INPUT_DIR / row["input_filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Input file not found")
    # StreamingResponse не объявляет Content-Length заранее — нет RuntimeError
    # при больших файлах (эта ошибка была с Response(content=data))
    data = path.read_bytes()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{row["input_filename"]}"'},
    )


@app.post("/api/complete/{job_id}")
async def complete_job(
    job_id: int,
    x_agent_token: str = Header(...),
    result: UploadFile = File(...),
):
    _auth(x_agent_token)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    # Имя файла берём ИЗ агента (он сам вычислил его с учётом brand/model/mode).
    # Подстраховка: если агент не передал filename или передал что-то опасное
    # (path traversal) — fallback на старую схему.
    incoming = (result.filename or "").strip()
    safe_name = Path(incoming).name  # отрезает любые ../ или /
    if safe_name and safe_name.lower().endswith(".png"):
        out_filename = safe_name
        # Если файл с таким именем уже есть (агент не учёл VPS-сторонние) —
        # добавим суффикс job_id чтобы не перезатереть.
        if (OUTPUT_DIR / out_filename).exists():
            stem = out_filename[:-4]
            out_filename = f"{stem}_j{job_id}.png"
    else:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_filename = f"split_{ts}_{job_id:03d}.png"

    out_path = OUTPUT_DIR / out_filename
    out_path.write_bytes(await result.read())

    # Архивируем входное фото
    src = INPUT_DIR / row["input_filename"]
    archived_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{row['input_filename']}"
    if src.exists():
        src.rename(PROCESSED_DIR / archived_name)

    with db_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', output_filename=?, archived_filename=?, updated_at=? WHERE id=?",
            (out_filename, archived_name, datetime.now().isoformat(), job_id),
        )
        conn.commit()

    log.info("Job %d complete → %s", job_id, out_filename)
    return {"ok": True, "output": out_filename}


@app.get("/api/agent-command")
def agent_command(x_agent_token: str = Header(...)):
    """Вотчдог на локальном ПК поллит сюда. Отдаём команду и сразу сбрасываем,
    чтобы она исполнилась ровно один раз."""
    _auth(x_agent_token)
    with db_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM flags WHERE key='agent_command'").fetchone()
        cmd = row["value"] if row else None
        if cmd:
            conn.execute("DELETE FROM flags WHERE key='agent_command'")
        conn.commit()
    return {"command": cmd or "none"}


@app.post("/api/worker/lease")
def worker_lease(
    x_agent_token: str = Header(...),
    worker_id: str = Form(...),
    priority: int = Form(100),
):
    """Воркер регистрирует heartbeat и спрашивает, активен ли он сейчас.
    active=True → можно брать задачи; False → standby (генерит другой)."""
    _auth(x_agent_token)
    now = datetime.now()
    with db_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workers ("
            "worker_id TEXT PRIMARY KEY, priority INTEGER NOT NULL DEFAULT 100, "
            "seen_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO workers (worker_id, priority, seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET priority=excluded.priority, "
            "seen_at=excluded.seen_at",
            (worker_id, priority, now.isoformat()),
        )
        conn.commit()
        rows = [dict(r) for r in conn.execute(
            "SELECT worker_id, priority, seen_at FROM workers"
        ).fetchall()]
    active_id = active_worker_id(rows, now, LEASE_TTL_SECONDS)
    return {"active": active_id == worker_id, "worker_id": worker_id,
            "active_worker": active_id}


@app.post("/api/heartbeat")
def agent_heartbeat(x_agent_token: str = Header(...)):
    """Агент вызывает при каждом цикле опроса — фиксируем время последнего контакта."""
    _auth(x_agent_token)
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO agent_heartbeat (id, seen_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at",
            (datetime.now().isoformat(),),
        )
        conn.commit()
    return {"ok": True}


@app.post("/api/fail/{job_id}")
async def fail_job(
    job_id: int,
    x_agent_token: str = Header(...),
    error: str = Form(...),
):
    _auth(x_agent_token)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    # Перемещаем входное фото в failed/ и запоминаем имя для кнопки «Повторить»
    failed_filename = None
    src = INPUT_DIR / row["input_filename"]
    if src.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        failed_filename = f"{ts}_{row['input_filename']}"
        src.rename(FAILED_DIR / failed_filename)

    with db_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status='failed', failed_filename=?, error_text=?, updated_at=? WHERE id=?",
            (failed_filename, error, datetime.now().isoformat(), job_id),
        )
        conn.commit()

    log.info("Job %d failed: %s", job_id, error)
    return {"ok": True}


@app.post("/api/submit-job")
async def submit_job(
    x_agent_token: str = Header(...),
    mode: str = Form("conditioner"),
    specs: str = Form(""),
    brand: str = Form(""),
    model: str = Form(""),
    chat_id: int = Form(...),
    caption: str = Form(""),
    photo: UploadFile = File(...),
):
    """Внешний клиент (Stock Bot) ставит задачу в очередь напрямую через API.

    Принимает фото товара + характеристики, создаёт pending-job в queue.db.
    Результат уйдёт в чат chat_id через result_sender бота (для conditioner —
    в режиме подтверждения). Возвращает имя сохранённого входного файла.
    """
    _auth(x_agent_token)

    if mode not in ("conditioner", "ritual", "wreath", "mcp", "kbt"):
        raise HTTPException(status_code=400, detail=f"Неизвестный режим: {mode}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    ext = Path(photo.filename or "input.jpg").suffix.lower() or ".jpg"
    filename = f"ext_{ts}{ext}"
    (INPUT_DIR / filename).write_bytes(await photo.read())

    log.info("submit-job: mode=%s brand=%s model=%s chat_id=%s file=%s",
             mode, brand, model, chat_id, filename)

    with db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs, brand, model, caption) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, filename, mode, specs or None, brand or None,
             model or None, caption or None),
        )
        conn.commit()

    return {"ok": True, "queued": filename}
