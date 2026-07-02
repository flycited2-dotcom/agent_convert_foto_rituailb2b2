"""Режим research: без эталонов, {{SPECS}} = «Категория Бренд Модель»."""
from __future__ import annotations
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import get_mode


def test_research_mode_exists_and_needs_no_reference():
    m = get_mode("research")
    assert m.key == "research"
    assert m.requires_reference is False
    assert m.reference_files == []


def test_render_prompt_substitutes_product():
    m = get_mode("research")
    p = m.render_prompt("Холодильник Beko B1RCSK362S")
    assert "Beko B1RCSK362S" in p


def test_other_modes_still_require_reference():
    assert get_mode("conditioner").requires_reference is True
