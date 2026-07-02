"""Чистый парсер УТП из текста ответа ChatGPT (research-задачи)."""
from __future__ import annotations
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent import parse_utp_lines


def test_extracts_check_lines():
    text = "Вот преимущества:\n✓ No Frost\n✓ Тихий 39 дБ\nНадеюсь, полезно!"
    assert parse_utp_lines(text) == ["✓ No Frost", "✓ Тихий 39 дБ"]


def test_normalizes_bullets_dashes_numbers():
    text = "- No Frost\n• Инверторный компрессор\n1. Класс A++\n2) Объём 320 л"
    assert parse_utp_lines(text) == [
        "✓ No Frost", "✓ Инверторный компрессор", "✓ Класс A++", "✓ Объём 320 л"]


def test_caps_at_max_items():
    text = "\n".join(f"- пункт {i}" for i in range(12))
    assert len(parse_utp_lines(text)) == 7


def test_strips_nested_markers_and_dedupes():
    # после сборки <li> строки приходят как «- ✓ …», а текст сообщения дублирует их
    text = "- ✓ No Frost\n- ✓ Тихий 39 дБ\n✓ No Frost"
    assert parse_utp_lines(text) == ["✓ No Frost", "✓ Тихий 39 дБ"]


def test_empty_when_no_list():
    assert parse_utp_lines("просто текст без списка") == []
    assert parse_utp_lines("") == []
    assert parse_utp_lines(None) == []
