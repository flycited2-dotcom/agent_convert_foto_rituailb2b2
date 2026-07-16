"""RATE_LIMIT_MODAL_JS — детект модалки «Слишком много запросов» (снята вживую
2026-07-16, акк1): «Вы отправляете запросы слишком часто. Доступ к вашим
диалогам временно ограничен в целях защиты данных.» + кнопка «Понятно».
Детект по ТЕКСТУ, не по классам: разметку OpenAI меняет чаще, чем формулировки."""
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent import RATE_LIMIT_MODAL_JS  # noqa: E402

MODAL_HTML = """
<html><body>
  <main><p>Обычный чат с длинным текстом сообщений про кондиционеры и миксеры.</p></main>
  <div role="dialog" style="width:400px;height:200px">
    <h2>Слишком много запросов</h2>
    <p>Вы отправляете запросы слишком часто. Доступ к вашим диалогам
       временно ограничен в целях защиты данных.</p>
    <p>Подождите несколько минут и повторите попытку.</p>
    <button>Понятно</button>
  </div>
</body></html>
"""

PLAIN_HTML = """
<html><body><main>
  <p>Готовая карточка товара, никаких ограничений. Запросов много не бывает.</p>
</main></body></html>
"""

EN_MODAL_HTML = """
<html><body>
  <div role="dialog" style="width:400px;height:150px">
    <h2>Too many requests</h2>
    <p>You are sending messages too quickly. Please wait a few minutes.</p>
    <button>Got it</button>
  </div>
</body></html>
"""


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        yield pg
        browser.close()


def test_detects_russian_modal(page):
    page.set_content(MODAL_HTML)
    text = page.evaluate(RATE_LIMIT_MODAL_JS)
    assert text and "Слишком много запросов" in text


def test_detects_english_modal(page):
    page.set_content(EN_MODAL_HTML)
    text = page.evaluate(RATE_LIMIT_MODAL_JS)
    assert text and "Too many requests" in text


def test_plain_page_no_false_positive(page):
    # «Запросов много не бывает» не должно триггерить (regex требует сигнатуру)
    page.set_content(PLAIN_HTML)
    assert page.evaluate(RATE_LIMIT_MODAL_JS) is None
