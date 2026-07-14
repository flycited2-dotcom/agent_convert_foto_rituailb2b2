"""Бренд/модель обязаны попадать в промпт (2026-07-14): kbt-задачи слали brand/model
только в имя файла, specs — без названия товара. Промпт велит «возьми бренд из
названия товара в этом сообщении» → модели неоткуда взять бренд, и она списывала
его с эталона (все миксеры вышли «HOMELINE RDF-260DD» — бренд эталона-холодильника)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import Mode  # noqa: E402


def _mode(prompt="ТЕКСТ ДЛЯ КАРТОЧКИ:\n{{SPECS}}"):
    return Mode(key="kbt", label="КБТ", project_url="http://x",
                reference_files=[], prompt=prompt)


def test_brand_model_prepended_to_specs():
    p = _mode().render_prompt("✓ Мощность 1000 Вт", brand="Vitek", model="VT-1443")
    assert "НАЗВАНИЕ ТОВАРА: Vitek VT-1443" in p
    assert "✓ Мощность 1000 Вт" in p
    # название — ПЕРЕД характеристиками (промпт ссылается на «название товара»)
    assert p.index("НАЗВАНИЕ ТОВАРА") < p.index("✓ Мощность")


def test_brand_only():
    p = _mode().render_prompt("✓ X", brand="Philips")
    assert "НАЗВАНИЕ ТОВАРА: Philips" in p


def test_no_brand_no_line():
    # без brand/model — прежнее поведение, никаких пустых строк-заголовков
    p = _mode().render_prompt("✓ X")
    assert "НАЗВАНИЕ ТОВАРА" not in p
    assert "✓ X" in p


def test_no_placeholder_prompt_unchanged():
    p = _mode(prompt="без плейсхолдера").render_prompt("✓ X", brand="Vitek")
    assert p == "без плейсхолдера"
