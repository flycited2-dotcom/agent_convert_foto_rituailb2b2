"""HTTP API для агента на локальном ПК.

Эндпоинты:
  GET  /api/next-job          → следующая задача или 204
  GET  /api/input/{job_id}    → скачать входное фото
  POST /api/complete/{job_id} → загрузить результат (multipart: result=<file>)
  POST /api/fail/{job_id}     → пометить как ошибку (form: error=<text>)

Запуск: uvicorn vps_api:app --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from config_vps import API_TOKEN, DB_PATH, FAILED_DIR, INPUT_DIR, OUTPUT_DIR, PROCESSED_DIR

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
    return {"id": row["id"], "input_filename": row["input_filename"]}


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
    # FileResponse вызывал RuntimeError "Response content longer than Content-Length"
    # Читаем файл в память и отдаём как Response — надёжнее
    from fastapi.responses import Response
    data = path.read_bytes()
    return Response(
        content=data,
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

    # Сохраняем результат
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_filename = f"ritual_{ts}_{job_id:03d}.png"
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
