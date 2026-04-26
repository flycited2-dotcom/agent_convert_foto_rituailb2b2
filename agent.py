"""Основной агент: подключается к Chrome через CDP, обрабатывает 1 файл.

Запуск:
    python agent.py <path_to_image>
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright

from clipboard_utils import copy_image_to_clipboard, copy_text_to_clipboard
from config import (
    CHROME_CDP_URL,
    GENERATION_TIMEOUT_SEC,
    LOGS_DIR,
    OUTPUT_DIR,
    PROCESSED_DIR,
    PROMPT_TEMPLATE,
    REFERENCE_FILES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("agent")


GENERATED_READY_SELECTOR = 'button[data-testid="good-image-turn-action-button"]'
GENERATED_IMG_SELECTOR = 'img[alt^="Сформированное"], img[alt^="Generated"]'
COMPOSER_SELECTOR = 'div[contenteditable="true"]'


async def find_or_open_chatgpt(browser: Browser) -> Page:
    for context in browser.contexts:
        for page in context.pages:
            if "chatgpt.com" in page.url or "chat.openai.com" in page.url:
                return page
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()
    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    return page


async def open_new_chat(page: Page) -> None:
    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    await page.wait_for_selector(COMPOSER_SELECTOR, timeout=20000)
    await asyncio.sleep(1)


async def paste_image(page: Page, image_path: Path, settle_seconds: float = 4.0) -> None:
    log.info("Paste image: %s", image_path.name)
    copy_image_to_clipboard(str(image_path))
    await page.locator(COMPOSER_SELECTOR).first.click()
    await asyncio.sleep(0.3)
    await page.keyboard.press("Control+V")
    await asyncio.sleep(settle_seconds)


async def paste_text(page: Page, text: str) -> None:
    copy_text_to_clipboard(text)
    await page.locator(COMPOSER_SELECTOR).first.click()
    await asyncio.sleep(0.3)
    await page.keyboard.press("Control+V")
    await asyncio.sleep(0.5)


async def submit(page: Page) -> None:
    # У ChatGPT с вложениями Enter не отправляет — нужен клик по send-button.
    # Кнопка disabled пока не догрузятся attachments — ждём enabled-состояния.
    btn = page.locator('button[data-testid="send-button"]:not([disabled])')
    await btn.wait_for(state="visible", timeout=60000)
    await btn.click()
    log.info("Submit нажат")


async def wait_for_generation(page: Page, timeout_ms: int) -> None:
    log.info("Жду результат до %d сек…", timeout_ms // 1000)
    # Кнопка "👍 хорошее изображение" появляется только когда картинка готова
    await page.wait_for_selector(
        GENERATED_READY_SELECTOR, state="visible", timeout=timeout_ms
    )
    log.info("Картинка готова, ждём полной загрузки пикселей…")
    # Дополнительно — дождаться img.complete && naturalWidth > 1000
    for _ in range(40):
        ready = await page.evaluate(
            """() => {
                const imgs = [...document.querySelectorAll('img')];
                const cand = imgs.filter(im => /^Сформированное|^Generated/.test(im.alt) && im.naturalWidth > 1000);
                if (!cand.length) return false;
                return cand.every(im => im.complete);
            }"""
        )
        if ready:
            await asyncio.sleep(1)
            return
        await asyncio.sleep(0.5)


async def download_via_anchor(page: Page, output_path: Path) -> None:
    """Качаем картинку через blob URL + временный <a download>, кликнутый Playwright'ом."""
    log.info("Готовлю blob и якорь для скачивания…")
    prepared = await page.evaluate(
        """async () => {
            const imgs = [...document.querySelectorAll('img')];
            const cand = imgs.filter(im => /^Сформированное|^Generated/.test(im.alt) && im.naturalWidth > 1000);
            const img = cand[cand.length - 1];
            if (!img) return {ok: false, error: 'no img'};
            try {
                const r = await fetch(img.src);
                if (!r.ok) return {ok: false, error: 'fetch ' + r.status};
                const blob = await r.blob();
                const url = URL.createObjectURL(blob);
                const old = document.getElementById('__agent_dl_anchor');
                if (old) old.remove();
                const a = document.createElement('a');
                a.id = '__agent_dl_anchor';
                a.href = url;
                a.download = 'output.png';
                a.textContent = 'DL';
                a.style.cssText = 'position:fixed;top:80px;left:300px;background:#22c55e;color:#fff;padding:24px 48px;font-size:24px;z-index:2147483647;border-radius:8px;font-family:sans-serif;';
                document.body.appendChild(a);
                window.__agent_blob_url = url;
                return {ok: true, size: blob.size, type: blob.type};
            } catch (e) {
                return {ok: false, error: String(e)};
            }
        }"""
    )
    if not prepared.get("ok"):
        raise RuntimeError(f"Не удалось подготовить blob: {prepared.get('error')}")

    log.info("Blob готов (%s байт). Кликаю якорь…", prepared.get("size"))
    async with page.expect_download(timeout=20000) as dl_info:
        await page.locator("#__agent_dl_anchor").click()
    download = await dl_info.value
    await download.save_as(str(output_path))
    log.info("Сохранено: %s", output_path)

    await page.evaluate(
        """() => {
            const a = document.getElementById('__agent_dl_anchor');
            if (a) a.remove();
            if (window.__agent_blob_url) {
                URL.revokeObjectURL(window.__agent_blob_url);
                delete window.__agent_blob_url;
            }
        }"""
    )


def make_output_path() -> Path:
    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H-%M-%S")
    existing = list(OUTPUT_DIR.glob(f"ritual_{date_part}_*.png"))
    seq = len(existing) + 1
    return OUTPUT_DIR / f"ritual_{date_part}_{time_part}_{seq:03d}.png"


def archive_input(file_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = PROCESSED_DIR / f"{ts}_{file_path.name}"
    file_path.rename(target)
    return target


async def process_one_file(file_path: Path) -> Path:
    output_path = make_output_path()
    log.info("=== Обработка: %s → %s ===", file_path.name, output_path.name)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CHROME_CDP_URL)
        try:
            page = await find_or_open_chatgpt(browser)
            await open_new_chat(page)

            for ref in REFERENCE_FILES:
                if not ref.exists():
                    raise FileNotFoundError(f"Эталон не найден: {ref}")
                await paste_image(page, ref)

            await paste_image(page, file_path, settle_seconds=5.0)
            await paste_text(page, PROMPT_TEMPLATE)
            await submit(page)

            await wait_for_generation(page, GENERATION_TIMEOUT_SEC * 1000)
            await download_via_anchor(page, output_path)
        finally:
            await browser.close()  # для CDP это disconnect, не убивает Chrome

    return output_path


async def main_cli() -> None:
    if len(sys.argv) < 2:
        print("Использование: python agent.py <путь_к_фото>")
        sys.exit(1)
    file_path = Path(sys.argv[1]).resolve()
    if not file_path.exists():
        print(f"Файл не найден: {file_path}")
        sys.exit(1)
    out = await process_one_file(file_path)
    archive_input(file_path)
    print(f"\nГотово: {out}")


if __name__ == "__main__":
    asyncio.run(main_cli())
