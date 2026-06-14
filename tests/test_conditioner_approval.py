"""Тесты режима подтверждения карточек кондиционера и постановки задачи из API.

Запуск: pytest tests/test_conditioner_approval.py -v

Реальный endpoint /api/submit-job тестируется через FastAPI TestClient, но
только там, где установлены fastapi + python-multipart (VPS/CI); локально
без них тест пропускается. Контракт INSERT и логика кнопок проверяются всегда.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# ─── Пути ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_VPS_DIR = _ROOT / "vps"
for _p in (_VPS_DIR, _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import vps_bot
from vps_bot import init_db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Перенаправляет DB-операции бота во временную базу (как в test_modes.py)."""
    db_path = tmp_path / "test_queue.db"
    monkeypatch.setattr(vps_bot, "DB_PATH", db_path)

    def _conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(vps_bot, "db_conn", _conn)
    init_db()
    return db_path, _conn


# ══════════════════════════════════════════════════════════════════════
# 1. Контракт INSERT эндпоинта /api/submit-job
#    (mirror INSERT, как в test_modes.py для redo/retry — не требует fastapi)
# ══════════════════════════════════════════════════════════════════════

def test_submit_job_insert_contract(isolated_db):
    """INSERT эндпоинта кладёт pending-задачу conditioner с specs/brand/model."""
    _, conn_fn = isolated_db
    with conn_fn() as conn:
        conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs, brand, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1264067528, "ext_x.jpg", "conditioner",
             "⚡ Класс A++\n❄️ Инверторная технология", "Samsung", "WindFree"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM jobs WHERE input_filename='ext_x.jpg'"
        ).fetchone()
    assert row["status"] == "pending"        # дефолт схемы — готова к обработке агентом
    assert row["mode"] == "conditioner"
    assert row["specs"].startswith("⚡ Класс A++")
    assert row["brand"] == "Samsung"
    assert row["model"] == "WindFree"
    assert row["result_sent"] == 0


def test_caption_column_stored_and_read(isolated_db):
    """Миграция добавила jobs.caption; подпись-прайс хранится и читается без потерь."""
    _, conn_fn = isolated_db
    cap = "<blockquote>❄️ Daichi\nBravo\n7 — 39 990 ₽</blockquote>"
    with conn_fn() as conn:
        conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, caption) VALUES (?, ?, ?, ?)",
            (1, "ext_cap.jpg", "conditioner", cap),
        )
        conn.commit()
        row = conn.execute(
            "SELECT caption FROM jobs WHERE input_filename='ext_cap.jpg'"
        ).fetchone()
    assert row["caption"] == cap


# ══════════════════════════════════════════════════════════════════════
# 2. Кнопки готовой карточки кондиционера (_conditioner_result_markup)
# ══════════════════════════════════════════════════════════════════════

def test_result_markup_with_channel():
    """Канал настроен → есть кнопка «Опубликовать» (publish) + redo + bad."""
    kb = vps_bot._conditioner_result_markup(42, has_channel=True)
    texts = [b.text for r in kb.inline_keyboard for b in r]
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert any("Опубликовать" in t for t in texts)
    assert "publish:42" in datas
    assert "redo:42" in datas
    assert "bad:42" in datas


def test_result_markup_without_channel():
    """Канала нет → кнопки публикации нет, но redo/bad остаются."""
    kb = vps_bot._conditioner_result_markup(42, has_channel=False)
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "publish:42" not in datas
    assert "redo:42" in datas
    assert "bad:42" in datas


# ══════════════════════════════════════════════════════════════════════
# 3. Реальный endpoint /api/submit-job (только где есть fastapi)
# ══════════════════════════════════════════════════════════════════════

def _api_client(tmp_path, monkeypatch):
    """Готовит TestClient + временную БД/INPUT_DIR для vps_api. Пропускает тест,
    если fastapi/python-multipart не установлены (локальная машина)."""
    pytest.importorskip("fastapi")
    pytest.importorskip("multipart")          # python-multipart (Form/File)
    from fastapi.testclient import TestClient
    import vps_api

    db_path = tmp_path / "api_queue.db"
    monkeypatch.setattr(vps_bot, "DB_PATH", db_path)

    def _conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(vps_bot, "db_conn", _conn)
    init_db()                                  # создаём схему jobs во временной БД
    monkeypatch.setattr(vps_api, "db_conn", _conn)
    monkeypatch.setattr(vps_api, "INPUT_DIR", tmp_path)
    monkeypatch.setattr(vps_api, "API_TOKEN", "")   # пусто → _auth пропускает любой токен
    return TestClient(vps_api.app), _conn


def test_submit_job_endpoint(tmp_path, monkeypatch):
    """POST /api/submit-job сохраняет фото и кладёт pending-задачу в БД."""
    client, conn_fn = _api_client(tmp_path, monkeypatch)
    cap = "<blockquote>❄️ Ballu\nOlympio\n9 — 45 990 ₽</blockquote>"
    resp = client.post(
        "/api/submit-job",
        headers={"x-agent-token": "anything"},
        data={"mode": "conditioner", "specs": "⚡ A++", "brand": "Ballu",
              "model": "Olympio", "chat_id": "1264067528", "caption": cap},
        files={"photo": ("p.jpg", b"\xff\xd8\xff", "image/jpeg")},
    )
    assert resp.status_code == 200, resp.text
    queued = resp.json()["queued"]
    assert (tmp_path / queued).exists()        # фото сохранено в INPUT_DIR
    with conn_fn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE input_filename=?", (queued,)
        ).fetchone()
    assert row["status"] == "pending"
    assert row["mode"] == "conditioner"
    assert row["brand"] == "Ballu"
    assert row["chat_id"] == 1264067528
    assert row["caption"] == cap               # подпись-прайс сохранена endpoint'ом


def test_submit_job_rejects_unknown_mode(tmp_path, monkeypatch):
    """Неизвестный режим → 400, задача не создаётся."""
    client, _ = _api_client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/submit-job",
        headers={"x-agent-token": "x"},
        data={"mode": "bogus", "chat_id": "1"},
        files={"photo": ("p.jpg", b"\xff", "image/jpeg")},
    )
    assert resp.status_code == 400
