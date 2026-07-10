"""FIND_GENERATED_IMG_JS против нового рендера ChatGPT (инцидент 2026-07-10).

Около полудня 10.07 ChatGPT перестал оборачивать сгенерированные картинки в
[data-message-author-role="assistant"] — они рисуются в треде «голыми»
(одиночные и A/B-пары «Какое изображение вам нравится больше?»). Селектор
находил ноль → «Изображение не появилось за 330 сек» при готовой карточке
в чате (задачи 856-862, дампы logs/timeout_wait_gen_20260710_*).

Фикстуры — реальные дампы страницы (sig в URL затёрт). DOM собирается по
скриншотам тех же дампов: аватарки в сайдбаре (вне main), загрузки — в
user-блоке, сгенерированные — в треде без role-обёртки.

Охранные тесты про «чужие» картинки — урок EUROHOFF 2026-07-07 (a2ec410):
fallback «любая свежая вне user» скачивал ленивые превью чужих генераций.
"""
import json
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent import FIND_GENERATED_IMG_JS  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures"

SINGLE = json.loads((_FIXTURES / "dump_20260710_123112_single.json").read_text(encoding="utf-8"))
AB_PAIR = json.loads((_FIXTURES / "dump_20260710_115603_ab_pair.json").read_text(encoding="utf-8"))

GEN_ALT_PREFIX = "Сформированное изображение"


def _img_tag(im: dict) -> str:
    # naturalWidth в тестовом DOM подделывается через data-nw (сеть отключена)
    return f'<img src="{im["src"]}" alt="{im["alt"]}" data-nw="{im["w"]}">'


def _html_from_dump(dump: list[dict]) -> str:
    """DOM новой вёрстки: сайдбар вне main, user-блок, тред без role-обёртки."""
    nav, user, thread = [], [], []
    for im in dump:
        if "cdn.auth0.com" in im["src"]:
            nav.append(_img_tag(im))
        elif im["parentRole"] == "user":
            user.append(_img_tag(im))
        else:
            thread.append(_img_tag(im))
    return (
        f"<nav>{''.join(nav)}</nav>"
        f"<main><div data-message-author-role=\"user\">{''.join(user)}</div>"
        f"{''.join(thread)}</main>"
    )


def _baseline(dump: list[dict]) -> list[str]:
    """Снимок после submit+settle: аватарки + загрузки пользователя."""
    return [im["src"] for im in dump
            if "cdn.auth0.com" in im["src"] or im["parentRole"] == "user"]


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        # Не ходим в сеть за картинками: размеры подделываются data-nw
        pg.route("**/*", lambda route: route.abort()
                 if route.request.resource_type == "image" else route.continue_())
        yield pg
        browser.close()


def _find_src(page, html: str, baseline: list[str]) -> str | None:
    page.set_content(html, wait_until="domcontentloaded")
    page.evaluate(
        """() => {
            for (const im of document.querySelectorAll('img')) {
                Object.defineProperty(im, 'naturalWidth', {value: +im.dataset.nw || 0});
                Object.defineProperty(im, 'complete', {value: true});
            }
        }"""
    )
    return page.evaluate(
        f"(baseline) => {{ const im = ({FIND_GENERATED_IMG_JS.strip()})(baseline);"
        f" return im ? im.src : null; }}",
        baseline,
    )


def test_new_markup_single_image(page):
    """Дамп 12:31 (задача 860, Sakura): одиночная карточка в треде без
    assistant-обёртки должна быть найдена."""
    src = _find_src(page, _html_from_dump(SINGLE), _baseline(SINGLE))
    assert src is not None, "сгенерированная карточка не найдена (баг 2026-07-10)"
    assert "file_00000000662c72438335601957fd4c32" in src


def test_new_markup_ab_pair_picks_last(page):
    """Дамп 11:56 (задача 856): A/B-пара «какое нравится больше» — качаем
    последний по DOM вариант (низ треда = самый свежий рендер)."""
    src = _find_src(page, _html_from_dump(AB_PAIR), _baseline(AB_PAIR))
    assert src is not None
    assert "file_0000000072b87246a37ef1826b0ff706" in src  # HOMELINE, второй в паре


def test_old_markup_assistant_block_has_priority(page):
    """Регресс: старая разметка с assistant-role работает и приоритетнее
    голых картинок треда."""
    html = (
        '<main><div data-message-author-role="user">'
        '<img src="https://x/upload.png" alt="u.png" data-nw="1254"></div>'
        f'<img src="https://x/naked.png" alt="{GEN_ALT_PREFIX}: тест" data-nw="1254">'
        '<div data-message-author-role="assistant">'
        '<img src="https://x/in-assistant.png" alt="" data-nw="1254"></div></main>'
    )
    src = _find_src(page, html, ["https://x/upload.png"])
    assert src == "https://x/in-assistant.png"


def test_no_generation_yet_returns_null(page):
    """До окончания генерации (на странице только baseline) — None, ждём дальше."""
    pre_gen = [im for im in SINGLE if GEN_ALT_PREFIX not in im["alt"]
               and im["alt"] != ""]
    src = _find_src(page, _html_from_dump(pre_gen), _baseline(pre_gen))
    assert src is None


def test_fresh_big_image_without_gen_alt_ignored(page):
    """Урок EUROHOFF: свежая крупная картинка вне user-блока, но БЕЗ метки
    «Сформированное изображение» — не наша, не качать."""
    html = (
        '<main><div data-message-author-role="user">'
        '<img src="https://x/upload.png" alt="u.png" data-nw="1254"></div>'
        '<img src="https://x/foreign.png" alt="" data-nw="1254"></main>'
    )
    src = _find_src(page, html, ["https://x/upload.png"])
    assert src is None


def test_stale_gen_alt_above_user_turn_ignored(page):
    """Ленивое превью прошлой генерации, подгрузившееся ВЫШЕ сообщения
    пользователя (шапка проекта), — не наше: ответ всегда ниже вопроса."""
    html = (
        f'<main><img src="https://x/stale.png" alt="{GEN_ALT_PREFIX}: старая" data-nw="1254">'
        '<div data-message-author-role="user">'
        '<img src="https://x/upload.png" alt="u.png" data-nw="1254"></div></main>'
    )
    src = _find_src(page, html, ["https://x/upload.png"])
    assert src is None


def test_stale_above_real_below_picks_real(page):
    """Превью старой генерации выше user-блока + настоящая карточка ниже —
    качаем настоящую."""
    html = (
        f'<main><img src="https://x/stale.png" alt="{GEN_ALT_PREFIX}: старая" data-nw="1254">'
        '<div data-message-author-role="user">'
        '<img src="https://x/upload.png" alt="u.png" data-nw="1254"></div>'
        f'<img src="https://x/real.png" alt="{GEN_ALT_PREFIX}: наша" data-nw="1254"></main>'
    )
    src = _find_src(page, html, ["https://x/upload.png"])
    assert src == "https://x/real.png"
