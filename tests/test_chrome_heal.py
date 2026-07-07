"""Самолечение «зомби-Chrome» (3 случая 06-07.07): /json/version отвечает,
но connect_over_cdp виснет 180с — задачи горели пачками, лечили руками.
Теперь агент сам: классифицирует attach-timeout → пересоздаёт СВОЙ Chrome →
задача уходит в requeue (не failed). Кулдаун — от вечного kill-цикла."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import remote_agent as ra  # noqa: E402


def test_is_cdp_attach_timeout_classification():
    zombie = Exception(
        "BrowserType.connect_over_cdp: Timeout 180000ms exceeded.\n"
        "Call log:\n  - <ws connected> ws://127.0.0.1:9333/devtools/...")
    assert ra._is_cdp_attach_timeout(zombie) is True
    assert ra._is_cdp_attach_timeout(Exception("Timeout 20000ms exceeded "
                                               "waiting for selector")) is False
    assert ra._is_cdp_attach_timeout(Exception("ECONNREFUSED")) is False


def test_heal_chrome_calls_kill_start_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(ra, "_kill_chrome_by_port", lambda: calls.append("kill"))
    monkeypatch.setattr(ra, "_start_chrome", lambda: calls.append("start"))
    monkeypatch.setattr(ra, "_wait_cdp", lambda timeout=30: calls.append("wait") or True)
    monkeypatch.setattr(ra, "_last_chrome_heal", None)
    assert ra.heal_chrome() is True
    assert calls == ["kill", "start", "wait"]


def test_heal_chrome_cooldown_blocks_repeat(monkeypatch):
    calls = []
    monkeypatch.setattr(ra, "_kill_chrome_by_port", lambda: calls.append("kill"))
    monkeypatch.setattr(ra, "_start_chrome", lambda: calls.append("start"))
    monkeypatch.setattr(ra, "_wait_cdp", lambda timeout=30: True)
    monkeypatch.setattr(ra, "_last_chrome_heal", None)
    assert ra.heal_chrome() is True
    assert ra.heal_chrome() is False          # кулдаун: не зацикливаем kill
    assert calls.count("kill") == 1


def test_heal_chrome_after_cooldown_allows(monkeypatch):
    monkeypatch.setattr(ra, "_kill_chrome_by_port", lambda: None)
    monkeypatch.setattr(ra, "_start_chrome", lambda: None)
    monkeypatch.setattr(ra, "_wait_cdp", lambda timeout=30: True)
    stale = datetime.now() - timedelta(seconds=ra._CDP_HEAL_COOLDOWN_SEC + 5)
    monkeypatch.setattr(ra, "_last_chrome_heal", stale)
    assert ra.heal_chrome() is True
