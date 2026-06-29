"""Чистая логика лиза: кто из воркеров сейчас активен. Без FastAPI/sqlite —
принимает строки как данные, тестируется изолированно."""
from __future__ import annotations
import os
from datetime import datetime

LEASE_TTL_SECONDS = int(os.getenv("LEASE_TTL_SECONDS", "900"))  # 15 минут


def active_worker_id(rows, now: datetime, ttl_seconds: int):
    """worker_id активного воркера или None.
    rows: итерируемое из mapping с ключами worker_id, priority, seen_at(iso).
    Активен среди свежих (now - seen_at <= ttl) тот, у кого наименьший priority;
    тай-брейк — наименьший worker_id (детерминированно)."""
    fresh = []
    for r in rows:
        try:
            seen = datetime.fromisoformat(r["seen_at"])
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if (now - seen).total_seconds() <= ttl_seconds:
            fresh.append((int(r["priority"]), str(r["worker_id"])))
    if not fresh:
        return None
    fresh.sort()  # сначала по priority, затем по worker_id
    return fresh[0][1]
