from __future__ import annotations
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VPS_DIR = _ROOT / "vps"
for _p in (_ROOT, _VPS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from worker_lease import active_worker_id  # vps/worker_lease.py (через _VPS_DIR в sys.path)

NOW = datetime(2026, 6, 30, 12, 0, 0)
TTL = 900  # 15 мин


def _row(wid, prio, seen):
    return {"worker_id": wid, "priority": prio, "seen_at": seen.isoformat()}


def test_desktop_wins_when_both_fresh():
    rows = [_row("desktop", 1, NOW), _row("laptop", 2, NOW)]
    assert active_worker_id(rows, NOW, TTL) == "desktop"


def test_laptop_takes_over_when_desktop_stale():
    rows = [_row("desktop", 1, NOW - timedelta(minutes=20)),
            _row("laptop", 2, NOW)]
    assert active_worker_id(rows, NOW, TTL) == "laptop"


def test_none_fresh_returns_none():
    rows = [_row("desktop", 1, NOW - timedelta(hours=2))]
    assert active_worker_id(rows, NOW, TTL) is None


def test_tiebreak_equal_priority_lexicographic():
    rows = [_row("bbb", 1, NOW), _row("aaa", 1, NOW)]
    assert active_worker_id(rows, NOW, TTL) == "aaa"


def test_bad_seen_at_ignored():
    rows = [_row("laptop", 2, NOW), {"worker_id": "x", "priority": 1, "seen_at": "broken"}]
    assert active_worker_id(rows, NOW, TTL) == "laptop"
