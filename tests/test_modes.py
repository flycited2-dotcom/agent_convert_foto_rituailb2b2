"""Тесты переключения режимов и характеристик (vps_bot).

Запуск: pytest tests/test_modes.py -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# ─── Пути ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent  # agent_convert_foto_rituailb2b2/
_VPS_DIR = _ROOT / "vps"
_AGENT_DIR = _ROOT

for _p in (_VPS_DIR, _AGENT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import vps_bot
from vps_bot import (
    DEFAULT_MODE,
    MODES_LABELS,
    MODES_WITH_SPECS,
    get_user_mode,
    get_user_specs,
    init_db,
    parse_brand_model,
    set_user_mode,
    set_user_specs,
)


# ─── Fixture: изолированная БД ────────────────────────────────────────

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Перенаправляет все DB-операции во временную базу данных."""
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
# 1. Переключение режима (set/get_user_mode)
# ══════════════════════════════════════════════════════════════════════

def test_get_default_mode(isolated_db):
    """Пользователь без записи в БД получает DEFAULT_MODE."""
    assert get_user_mode(999999) == DEFAULT_MODE


@pytest.mark.parametrize("mode", list(MODES_LABELS.keys()))
def test_set_get_user_mode_all_modes(isolated_db, mode):
    """set_user_mode → get_user_mode корректен для всех 5 режимов."""
    set_user_mode(100, mode)
    assert get_user_mode(100) == mode


def test_mode_persists_after_reconnect(isolated_db):
    """Режим сохраняется после повторного открытия соединения с БД."""
    set_user_mode(200, "kbt")
    assert get_user_mode(200) == "kbt"


def test_invalid_mode_not_saved(isolated_db):
    """Неизвестный ключ режима не записывается — возвращается DEFAULT_MODE."""
    set_user_mode(300, "unknown_mode")
    assert get_user_mode(300) == DEFAULT_MODE


def test_mode_overwrite(isolated_db):
    """Режим перезаписывается при повторном вызове set_user_mode."""
    set_user_mode(400, "ritual")
    set_user_mode(400, "conditioner")
    assert get_user_mode(400) == "conditioner"


# ══════════════════════════════════════════════════════════════════════
# 2. Хранение характеристик (set/get_user_specs)
# ══════════════════════════════════════════════════════════════════════

def test_specs_default_is_none(isolated_db):
    """Без записи get_user_specs возвращает None."""
    assert get_user_specs(999999, "conditioner") is None


@pytest.mark.parametrize("mode", sorted(MODES_WITH_SPECS))
def test_set_get_specs_per_mode(isolated_db, mode):
    """Specs хранятся отдельно по каждому режиму через JSON-объект."""
    set_user_specs(500, f"Specs for {mode}", mode)
    assert get_user_specs(500, mode) == f"Specs for {mode}"


def test_specs_isolated_per_mode(isolated_db):
    """Specs для разных режимов не перекрываются."""
    set_user_specs(600, "conditioner specs", "conditioner")
    set_user_specs(600, "kbt specs", "kbt")
    assert get_user_specs(600, "conditioner") == "conditioner specs"
    assert get_user_specs(600, "kbt") == "kbt specs"
    assert get_user_specs(600, "mcp") is None


def test_specs_reset(isolated_db):
    """set_user_specs(None) удаляет запись для конкретного режима."""
    set_user_specs(700, "Some specs", "conditioner")
    set_user_specs(700, None, "conditioner")
    assert get_user_specs(700, "conditioner") is None


def test_specs_backward_compat(isolated_db):
    """Старый формат (plain-строка без JSON) читается без ошибки."""
    db_path, _ = isolated_db
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO user_state (chat_id, mode, pending_specs) VALUES (?, ?, ?)",
        (800, "ritual", "plain string specs"),
    )
    conn.commit()
    conn.close()
    result = get_user_specs(800, "ritual")
    assert result == "plain string specs"


# ══════════════════════════════════════════════════════════════════════
# 3. parse_brand_model
# ══════════════════════════════════════════════════════════════════════

def test_parse_none_input():
    """None → (None, None, '')."""
    assert parse_brand_model(None) == (None, None, "")


def test_parse_explicit_brand_model():
    """Явные «Бренд: X» / «Модель: X» правильно извлекаются."""
    specs = "Бренд: Midea\nМодель: MSAC-12HRN1\nИнверторный компрессор"
    brand, model, cleaned = parse_brand_model(specs)
    assert brand == "Midea"
    assert model == "MSAC-12HRN1"
    assert "Инверторный компрессор" in cleaned


def test_parse_device_type_extracts_brand():
    """Тип устройства в начале → следующее слово = brand."""
    specs = "Холодильник LG GC-B257JLYV\nNo Frost\nA++"
    brand, model, _ = parse_brand_model(specs)
    assert brand == "LG"


def test_parse_fallback_first_word():
    """Fallback: первое слово первой строки → brand."""
    specs = "Samsung Galaxy A55 5G 8/256GB"
    brand, model, _ = parse_brand_model(specs)
    assert brand == "Samsung"
    assert model is not None


def test_parse_english_prefix():
    """Brand:/Model: (английский) также распознаётся."""
    specs = "Brand: Haier\nModel: AS09NS5ERA\nECO режим"
    brand, model, _ = parse_brand_model(specs)
    assert brand == "Haier"
    assert model == "AS09NS5ERA"


# ══════════════════════════════════════════════════════════════════════
# 4. Mode.render_prompt (config.Mode)
# ══════════════════════════════════════════════════════════════════════

def _make_mode(**kw):
    from config import Mode
    defaults = dict(
        key="test", label="Test", project_url="https://x.com",
        reference_files=[], prompt="{{SPECS}}",
        requires_specs=True, default_specs="дефолт",
    )
    defaults.update(kw)
    return Mode(**defaults)


def test_render_prompt_substitutes_specs():
    """{{SPECS}} заменяется переданными характеристиками."""
    m = _make_mode(prompt="Товар:\n{{SPECS}}\nКонец")
    assert m.render_prompt("Мои характеристики") == "Товар:\nМои характеристики\nКонец"


def test_render_prompt_uses_default_on_none():
    """Если specs=None → подставляется default_specs."""
    m = _make_mode(default_specs="дефолт")
    assert m.render_prompt(None) == "дефолт"


def test_render_prompt_uses_default_on_empty_string():
    """Если specs='' → подставляется default_specs."""
    m = _make_mode(default_specs="дефолт")
    assert m.render_prompt("") == "дефолт"


def test_render_prompt_no_placeholder():
    """Промпт без {{SPECS}} возвращается без изменений."""
    m = _make_mode(prompt="Обычный промпт без плейсхолдера")
    assert m.render_prompt("любые specs") == "Обычный промпт без плейсхолдера"


# ══════════════════════════════════════════════════════════════════════
# 5. redo / retry сохраняют mode + specs + brand + model
# ══════════════════════════════════════════════════════════════════════

def _insert_job(conn_fn, **kw):
    with conn_fn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs, brand, model) "
            "VALUES (:chat_id, :input_filename, :mode, :specs, :brand, :model)",
            kw,
        )
        conn.commit()
        return cur.lastrowid


def _get_job(conn_fn, job_id):
    with conn_fn() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def _get_job_by_filename(conn_fn, filename):
    with conn_fn() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE input_filename=?", (filename,)
        ).fetchone()


@pytest.mark.parametrize("action", ["redo", "retry"])
def test_action_preserves_mode_specs_brand_model(isolated_db, action):
    """Новая задача от redo/retry содержит те же mode/specs/brand/model."""
    _, conn_fn = isolated_db

    orig_id = _insert_job(
        conn_fn,
        chat_id=123,
        input_filename="original.jpg",
        mode="kbt",
        specs="LG LRSOS2706S\nNo Frost",
        brand="LG",
        model="LRSOS2706S",
    )
    original = _get_job(conn_fn, orig_id)

    # Имитируем исправленный код on_callback для redo/retry
    new_filename = f"{action}_copy.jpg"
    with conn_fn() as conn:
        conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs, brand, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (123, new_filename,
             original["mode"], original["specs"],
             original["brand"], original["model"]),
        )
        conn.commit()

    new_job = _get_job_by_filename(conn_fn, new_filename)
    assert new_job["mode"] == "kbt"
    assert new_job["specs"] == "LG LRSOS2706S\nNo Frost"
    assert new_job["brand"] == "LG"
    assert new_job["model"] == "LRSOS2706S"


@pytest.mark.parametrize("action", ["redo", "retry"])
def test_action_preserves_conditioner_mode(isolated_db, action):
    """Повтор задачи кондиционера сохраняет режим conditioner и specs."""
    _, conn_fn = isolated_db

    orig_id = _insert_job(
        conn_fn,
        chat_id=456,
        input_filename="cond.jpg",
        mode="conditioner",
        specs="Бренд: Midea\nМодель: MSAC-12HRN1\nИнвертор",
        brand="Midea",
        model="MSAC-12HRN1",
    )
    original = _get_job(conn_fn, orig_id)

    new_filename = f"{action}_cond.jpg"
    with conn_fn() as conn:
        conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs, brand, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (456, new_filename,
             original["mode"], original["specs"],
             original["brand"], original["model"]),
        )
        conn.commit()

    new_job = _get_job_by_filename(conn_fn, new_filename)
    assert new_job["mode"] == "conditioner"
    assert new_job["brand"] == "Midea"
    assert new_job["model"] == "MSAC-12HRN1"
