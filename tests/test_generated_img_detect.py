"""Детект сгенерированной карточки в DOM ChatGPT (FIND_GENERATED_IMG_JS).

08.07.2026: ChatGPT сменил разметку — реплики теперь `section[data-turn="assistant"]`,
а старый селектор `data-message-author-role="assistant"` их не находит → генерация
вставала («Изображение не появилось за 210 сек») на всех дорожках. Тест ловит регресс:
детект обязан находить картинку в НОВОЙ и в LEGACY разметке и не хватать чужое фото
из user-сообщения (превью эталонов).

Прогоняем реальный JS в headless-Chromium через set_content; naturalWidth/complete
мокаем (в set_content настоящих пикселей нет). Скип, если Chromium для playwright
не установлен."""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

playwright_sync = pytest.importorskip("playwright.sync_api")
from agent import FIND_GENERATED_IMG_JS  # noqa: E402

# Мок размеров: в set_content картинки не грузятся, поэтому naturalWidth/complete
# проставляем из data-nw. src берём из data-src (иначе относительный src ломается).
_MOCK_SIZES = """
() => {
  document.querySelectorAll('img[data-nw]').forEach(im => {
    const nw = +im.getAttribute('data-nw');
    Object.defineProperty(im, 'naturalWidth', {get: () => nw, configurable: true});
    Object.defineProperty(im, 'complete', {get: () => true, configurable: true});
    Object.defineProperty(im, 'src', {get: () => im.getAttribute('data-src') || '',
                                      configurable: true});
  });
}
"""

# Новая разметка ChatGPT: section[data-turn]. User-турн с превью эталона (крупное!) +
# assistant-турн с готовой карточкой.
NEW_MARKUP = """
<section data-turn="user" data-testid="conversation-turn-1">
  <div data-message-author-role="user">
    <img data-id="etalon" data-nw="900" data-src="blob:etalon-preview">
  </div>
</section>
<section data-turn="assistant" data-testid="conversation-turn-2">
  <div class="markdown"><p>Готово</p>
    <img data-id="card" data-nw="1254" data-src="https://chatgpt.com/backend-api/card.png">
  </div>
</section>
"""

# Legacy-разметка: старый атрибут роли (совместимость на случай A/B ChatGPT).
LEGACY_MARKUP = """
<div data-message-author-role="user">
  <img data-id="etalon" data-nw="900" data-src="blob:etalon-preview">
</div>
<div data-message-author-role="assistant">
  <img data-id="card" data-nw="1254" data-src="https://chatgpt.com/backend-api/card.png">
</div>
"""

# Только user-сообщение с крупным превью — чужое фото, брать НЕЛЬЗЯ.
USER_ONLY_MARKUP = """
<section data-turn="user" data-testid="conversation-turn-1">
  <div data-message-author-role="user">
    <img data-id="tv-photo" data-nw="1200" data-src="blob:foreign-tv">
  </div>
</section>
"""

# Обёртка: прогнать детект и вернуть data-id найденной картинки (или None).
_RUN = ("(baseline) => { const f = " + FIND_GENERATED_IMG_JS.strip()
        + "; const r = f(baseline); return r ? r.getAttribute('data-id') : null; }")


@pytest.fixture(scope="module")
def page():
    try:
        pw = playwright_sync.sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as e:                       # нет скачанного chromium в окружении
        pytest.skip(f"headless Chromium недоступен: {e}")
    pg = browser.new_page()
    yield pg
    browser.close()
    pw.stop()


def _detect(page, markup, baseline=None):
    page.set_content(f"<body>{markup}</body>")
    page.evaluate(_MOCK_SIZES)
    return page.evaluate(_RUN, baseline or [])


def test_new_markup_finds_card_not_etalon(page):
    assert _detect(page, NEW_MARKUP) == "card"


def test_legacy_markup_still_works(page):
    assert _detect(page, LEGACY_MARKUP) == "card"


def test_user_only_returns_none(page):
    # Чужое фото в user-сообщении не должно уехать в карточку.
    assert _detect(page, USER_ONLY_MARKUP) is None


def test_baseline_filters_old_image(page):
    # Карточка уже была на странице до submit (в baseline) → это не новая генерация.
    assert _detect(page, NEW_MARKUP,
                   baseline=["https://chatgpt.com/backend-api/card.png"]) is None
