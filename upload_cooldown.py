"""Кулдаун при исчерпании лимита загрузок ChatGPT ("Максимальное количество загрузок
0 за раз"). Без него агент долбит ретраями в тот момент, когда квота меньше всего
готова восстановиться (retry-шторм не даёт лимиту сброситься — инцидент 2026-07-03).

Персистентность — простой текстовый файл (ISO-таймстамп), переживает перезапуск
процесса/ПК: если агент упал прямо во время кулдауна, при следующем старте он
не ринется генерировать заново."""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path

# Реальный текст баннера OpenAI (RU-локаль), подтверждено скриншотом 2026-07-03.
UPLOAD_LIMIT_MARKERS = ("Максимальное количество загрузок",)


def is_upload_limit_error(text: str) -> bool:
    return any(marker in (text or "") for marker in UPLOAD_LIMIT_MARKERS)


def write_cooldown(path: Path, until: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(until.isoformat(), encoding="utf-8")


def read_cooldown(path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def in_cooldown(path: Path, now: datetime) -> bool:
    until = read_cooldown(path)
    return until is not None and now < until


def start_cooldown(path: Path, now: datetime, minutes: int) -> datetime:
    until = now + timedelta(minutes=minutes)
    write_cooldown(path, until)
    return until
