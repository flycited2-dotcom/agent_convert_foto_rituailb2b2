"""Ручной стоп сильнее автопробуждения (2026-07-15): cf-cards бродкастил 'start'
на все машины каждые полчаса — вотчдог трактовал его как кнопку владельца и
поднимал остановленные вручную дорожки (жалоба: «жму стоп — он поднимает»).
Теперь автокоманда = 'wake': поднимает ТОЛЬКО упавшие running-дорожки; для
остановленных вручную — напоминание владельцу, не старт."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent_watchdog import plan_wake  # noqa: E402


def test_wake_starts_only_dead_running_lanes():
    entries = [("desktop-a1", "running", False),   # должна работать, упала → старт
               ("desktop-a2", "running", True)]    # работает → не трогаем
    start, denied = plan_wake(entries)
    assert start == ["desktop-a1"]
    assert denied == []


def test_wake_respects_manual_stop():
    entries = [("desktop-a1", "stopped", False),
               ("desktop-a2", "stopped", False)]
    start, denied = plan_wake(entries)
    assert start == []
    assert denied == ["desktop-a1", "desktop-a2"]   # → напоминание владельцу


def test_wake_mixed():
    entries = [("desktop-a1", "stopped", False),    # ручной стоп → игнор
               ("desktop-a2", "running", False)]    # упала → старт
    start, denied = plan_wake(entries)
    assert start == ["desktop-a2"]
    assert denied == ["desktop-a1"]


def test_wake_healthy_running_noop():
    start, denied = plan_wake([("desktop-a1", "running", True)])
    assert start == [] and denied == []
