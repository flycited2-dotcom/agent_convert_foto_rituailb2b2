"""Переключатель «только ноут» / «только десктоп»: кнопки бота сейчас управляют
ОБЕИМИ машинами разом (_set_agent_flag пишет один и тот же command в оба ключа).
_set_exclusive_worker гарантирует, что активна ровно одна машина — другая
получает stop в том же атомарном действии."""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_VPS_DIR = _ROOT / "vps"
for _p in (_ROOT, _VPS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _bot_with_db(tmp_path, monkeypatch):
    pytest.importorskip("telegram")  # python-telegram-bot может не стоять локально
    import vps_bot
    db = tmp_path / "q.db"

    def _conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(vps_bot, "db_conn", _conn)
    return vps_bot, db


def _flags(db) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT)")
    rows = con.execute("SELECT key, value FROM flags").fetchall()
    con.close()
    return {r["key"]: r["value"] for r in rows}


# Десктоп получает команду по ДВУМ каналам: agent_command (legacy, старый вотчдог)
# и agent_command_desktop (адресный — после перехода на worker=desktop). Иначе
# при переключении десктопа на адресный ключ он оглох бы для кнопок бота.
def test_only_laptop_starts_laptop_stops_desktop(tmp_path, monkeypatch):
    bot, db = _bot_with_db(tmp_path, monkeypatch)
    bot._set_exclusive_worker("laptop")
    assert _flags(db) == {"agent_command_laptop": "start",
                          "agent_command_desktop": "stop", "agent_command": "stop"}


def test_only_desktop_starts_desktop_stops_laptop(tmp_path, monkeypatch):
    bot, db = _bot_with_db(tmp_path, monkeypatch)
    bot._set_exclusive_worker("desktop")
    assert _flags(db) == {"agent_command_laptop": "stop",
                          "agent_command_desktop": "start", "agent_command": "start"}


def test_switch_is_idempotent_overwrites_previous(tmp_path, monkeypatch):
    bot, db = _bot_with_db(tmp_path, monkeypatch)
    bot._set_exclusive_worker("desktop")
    bot._set_exclusive_worker("laptop")           # передумали — переключили обратно
    assert _flags(db) == {"agent_command_laptop": "start",
                          "agent_command_desktop": "stop", "agent_command": "stop"}


def test_rejects_unknown_worker_name(tmp_path, monkeypatch):
    bot, db = _bot_with_db(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        bot._set_exclusive_worker("phone")
