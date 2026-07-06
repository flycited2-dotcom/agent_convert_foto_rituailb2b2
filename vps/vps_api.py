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
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import io

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from config_vps import API_TOKEN, DB_PATH, FAILED_DIR, INPUT_DIR, OUTPUT_DIR, PROCESSED_DIR
from worker_lease import LEASE_TTL_SECONDS, active_worker_id

log = logging.getLogger("vps_api")
app = FastAPI(docs_url=None, redoc_url=None)  # отключаем Swagger UI в prod

# Аренда claim'а задачи: processing-задача, чья дорожка молчит дольше LEASE,
# возвращается в pending (дорожка умерла/зависла). 45 мин по умолчанию —
# генерация с 3 ретраями может идти 15-20 мин, у живого медленного агента
# задачу не выдёргиваем.
JOB_LEASE_SECONDS = int(os.getenv("AGENT_JOB_LEASE_SECONDS", "2700"))


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


_claim_cols_ready = False


def _ensure_claim_columns(conn: sqlite3.Connection) -> None:
    """Идемпотентная миграция: колонки claimed_by/claimed_at в jobs (кто и когда
    захватил задачу — фундамент мульти-дорожек). ALTER на существующей таблице,
    не только CREATE — грабля content-factory 2026-07-05 (крэш-луп на проде)."""
    global _claim_cols_ready
    if _claim_cols_ready:
        return
    for ddl in ("ALTER TABLE jobs ADD COLUMN claimed_by TEXT",
                "ALTER TABLE jobs ADD COLUMN claimed_at TEXT"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass                                  # колонка уже есть
    _claim_cols_ready = True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/next-job")
def next_job(x_agent_token: str = Header(...), caps: str = "", lane: str = "",
             modes: str = ""):
    _auth(x_agent_token)
    # caps — возможности агента (напр. "research"). Старые агенты параметр не шлют —
    # research-задачи им НЕ отдаём (не умеют и фейлили бы их «битой задачей»);
    # обычные карточки отдаём всем, как раньше.
    # lane — id дорожки (мульти-аккаунт, Phase 1): пишется в claimed_by.
    # modes — allowlist режимов дорожки (напр. "mcp,kbt,research"): у акк2 нет
    # проектов всех режимов — чужой mode открыл бы acc1-проект в acc2-сессии
    # и падал. Пусто = все режимы (как раньше).
    where = "status='pending'"
    args: list = []
    if "research" not in (caps or "").split(","):
        where += " AND mode != 'research'"
    allow = [m.strip() for m in (modes or "").split(",") if m.strip()]
    if allow:
        where += f" AND mode IN ({','.join('?' * len(allow))})"
        args = allow
    now = datetime.now()
    with db_conn() as conn:
        _ensure_claim_columns(conn)
        # Аренда: задачи умерших дорожек — обратно в пул. claimed_at IS NULL не
        # трогаем: это строки, взятые ДО апдейта (над ними может прямо сейчас
        # работать агент) — их вернёт только явный /api/requeue.
        cutoff = (now - timedelta(seconds=JOB_LEASE_SECONDS)).isoformat()
        conn.execute(
            "UPDATE jobs SET status='pending', claimed_by=NULL, claimed_at=NULL, "
            "updated_at=? WHERE status='processing' AND claimed_at IS NOT NULL "
            "AND claimed_at < ?", (now.isoformat(), cutoff))
        # Атомарный claim: выбор и захват ОДНИМ запросом (SQLite ≥3.35, на VPS
        # 3.45) — две дорожки не получат одну задачу. Раньше SELECT и UPDATE шли
        # раздельно — параллельные агенты хватали один и тот же job.
        row = conn.execute(
            f"UPDATE jobs SET status='processing', claimed_by=?, claimed_at=?, "
            f"updated_at=? WHERE id=(SELECT id FROM jobs WHERE {where} "
            f"ORDER BY id LIMIT 1) RETURNING *",
            (lane or None, now.isoformat(), now.isoformat(), *args)).fetchone()
        conn.commit()
    if not row:
        return JSONResponse(status_code=204, content=None)
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


@app.post("/api/requeue/{job_id}")
def requeue_job(job_id: int, x_agent_token: str = Header(...)):
    """Мягкий возврат задачи в очередь (дорожка упёрлась в лимит ChatGPT и уходит
    остывать): status='pending', claim снят, попытка НЕ сожжена (это не /api/fail).
    404 — задачи нет или она не processing (двойной requeue не ломает pending)."""
    _auth(x_agent_token)
    with db_conn() as conn:
        _ensure_claim_columns(conn)
        cur = conn.execute(
            "UPDATE jobs SET status='pending', claimed_by=NULL, claimed_at=NULL, "
            "updated_at=? WHERE id=? AND status='processing'",
            (datetime.now().isoformat(), job_id))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Job not found or not processing")
    return {"ok": True}


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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # каталоги иногда пропадают (2026-07-02/03)
    out_path.write_bytes(await result.read())

    # Архивируем входное фото
    src = INPUT_DIR / row["input_filename"]
    archived_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{row['input_filename']}"
    if src.exists():
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
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
def agent_command(x_agent_token: str = Header(...), worker: str = ""):
    """Вотчдог на локальном ПК поллит сюда. Отдаём команду и сразу сбрасываем,
    чтобы она исполнилась ровно один раз. `worker` — адресный флаг конкретной
    машины (agent_command_<worker>); без параметра — общий ключ agent_command
    (старые вотчдоги: десктоп до Task 7). Так команды не перехватываются
    чужой машиной (конфликт десктоп/ноут 2026-07-03)."""
    _auth(x_agent_token)
    key = f"agent_command_{worker}" if worker else "agent_command"
    with db_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM flags WHERE key=?", (key,)).fetchone()
        cmd = row["value"] if row else None
        if cmd:
            conn.execute("DELETE FROM flags WHERE key=?", (key,))
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
def agent_heartbeat(x_agent_token: str = Header(...), lane: str = ""):
    """Агент вызывает при каждом цикле опроса — фиксируем время последнего контакта.
    lane — id дорожки (Phase 3): пишем и общую строку (id=1, её читает статус
    vps_bot — совместимость), и per-lane строку в НОВУЮ таблицу lane_heartbeat
    (PK у agent_heartbeat не альтерится — SQLite не умеет менять PK)."""
    _auth(x_agent_token)
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO agent_heartbeat (id, seen_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at",
            (datetime.now().isoformat(),),
        )
        conn.execute("CREATE TABLE IF NOT EXISTS lane_heartbeat "
                     "(lane TEXT PRIMARY KEY, seen_at TEXT)")
        if lane:
            conn.execute(
                "INSERT INTO lane_heartbeat (lane, seen_at) VALUES (?, ?) "
                "ON CONFLICT(lane) DO UPDATE SET seen_at=excluded.seen_at",
                (lane, datetime.now().isoformat()),
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
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
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
    INPUT_DIR.mkdir(parents=True, exist_ok=True)   # каталог иногда пропадает (2026-07-02/03)
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


@app.post("/api/submit-research")
async def submit_research(
    x_agent_token: str = Header(...),
    brand: str = Form(""),
    model: str = Form(...),
    category: str = Form(""),
    chat_id: int = Form(...),
):
    """content-factory ставит research-задачу: по наименованию найти фото + УТП.
    Входного фото НЕТ (input_filename='') — агент ветвится по mode='research'
    ДО проверки битой задачи. category кладём в specs (промпту нужен тип товара)."""
    _auth(x_agent_token)
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs, brand, model) "
            "VALUES (?, '', 'research', ?, ?, ?)",
            (chat_id, category or None, brand or None, model),
        )
        conn.commit()
        job_id = cur.lastrowid
    log.info("submit-research: brand=%s model=%s → job %d", brand, model, job_id)
    return {"ok": True, "job_id": job_id}


@app.post("/api/complete-research/{job_id}")
async def complete_research(
    job_id: int,
    x_agent_token: str = Header(...),
    utp: str = Form(""),
    photo: UploadFile | None = File(None),
):
    """Агент возвращает результат research: текст УТП + (опционально) фото товара.
    result_sent=1 сразу — result_sender бота эти задачи не рассылает
    (их забирает content-factory из queue.db/OUTPUT_DIR)."""
    _auth(x_agent_token)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    out_filename = None
    if photo is not None:
        ext = Path(photo.filename or "r.png").suffix.lower() or ".png"
        out_filename = f"research_{job_id}{ext}"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / out_filename).write_bytes(await photo.read())
    with db_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', output_filename=?, result_specs=?, "
            "result_sent=1, updated_at=? WHERE id=?",
            (out_filename, utp or None, datetime.now().isoformat(), job_id),
        )
        conn.commit()
    log.info("Research %d done: фото=%s, УТП=%d симв.", job_id, out_filename, len(utp or ""))
    return {"ok": True, "output": out_filename}
