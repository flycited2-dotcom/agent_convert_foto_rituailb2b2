"""Основной агент: подключается к Chrome через CDP, обрабатывает 1 файл.

Запуск:
    python agent.py <path_to_image>
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright

from clipboard_utils import copy_image_to_clipboard, copy_text_to_clipboard
from config import (
    CHROME_CDP_URL,
    DEFAULT_MODE,
    GENERATION_TIMEOUT_SEC,
    LOGS_DIR,
    OUTPUT_DIR,
    PROCESSED_DIR,
    get_mode,
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


COMPOSER_SELECTOR = 'div[contenteditable="true"]'

# JS: возвращает <img> СГЕНЕРИРОВАННОЙ картинки.
#
# baselineList — src которые БЫЛИ на странице после submit+5s settle.
# Все user-фото к этому моменту уже имеют финальные URL и попадают сюда.
#
# Стратегия 1 (приоритет): картинка внутри последнего assistant-message.
#   Селекторы: [data-message-author-role="assistant"], [data-author-role="assistant"],
#   [data-testid^="conversation-turn-"][data-turn="assistant"].
#
# Стратегия 2 (fallback, если разметка assistant-role изменилась):
#   Любая крупная свежая картинка, которая НЕ внутри role="user" контейнера.
#   Это защищает от input-фото (они физически в role="user").
FIND_GENERATED_IMG_JS = """
    (baselineList) => {
        const baseline = new Set(baselineList || []);
        const isFresh = im => im.complete && im.naturalWidth > 600
                              && im.src && !baseline.has(im.src);

        // Стратегия 1: assistant role
        const aSelector = [
            '[data-message-author-role=\\"assistant\\"]',
            '[data-author-role=\\"assistant\\"]'
        ].join(', ');
        const aMsgs = [...document.querySelectorAll(aSelector)];
        if (aMsgs.length) {
            const last = aMsgs[aMsgs.length - 1];
            const cand = [...last.querySelectorAll('img')].filter(isFresh);
            if (cand.length) return cand[cand.length - 1];
        }

        // Стратегия 2 (fallback): крупная свежая картинка ВНЕ user-сообщений
        const uSelector = [
            '[data-message-author-role=\\"user\\"]',
            '[data-author-role=\\"user\\"]'
        ].join(', ');
        const uMsgs = [...document.querySelectorAll(uSelector)];
        const inUser = im => uMsgs.some(m => m.contains(im));

        const all = [...document.querySelectorAll('img')]
            .filter(im => isFresh(im) && !inUser(im));
        if (all.length) {
            return all.sort((a, b) => b.naturalWidth - a.naturalWidth)[0];
        }
        return null;
    }
"""


async def snapshot_image_srcs(page: Page) -> list[str]:
    """Снимок всех src картинок на странице — чтобы потом отфильтровать новую."""
    return await page.evaluate(
        "() => [...document.querySelectorAll('img')].map(im => im.src).filter(Boolean)"
    )


async def find_or_open_chatgpt(browser: Browser) -> Page:
    for context in browser.contexts:
        for page in context.pages:
            if "chatgpt.com" in page.url or "chat.openai.com" in page.url:
                return page
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()
    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    return page


async def open_new_chat(page: Page, url: str = "https://chatgpt.com/") -> None:
    await page.goto(url, wait_until="domcontentloaded")
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
    """Ждём пока кнопка send-button станет кликабельной, затем нажимаем.

    ChatGPT блокирует кнопку двумя способами (меняется от версии к версии):
      - HTML-атрибут  disabled
      - aria-disabled="true"
    Проверяем оба, ждём до 120 сек (3 фото на медленном канале).
    """
    btn = page.locator('button[data-testid="send-button"]')
    await btn.wait_for(state="visible", timeout=30000)  # кнопка должна появиться быстро

    for elapsed in range(120):
        is_disabled = await btn.evaluate(
            "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
        )
        if not is_disabled:
            break
        if elapsed % 15 == 0:
            log.info("Жду готовности send-button… %d сек", elapsed)
        await asyncio.sleep(1)
    else:
        raise RuntimeError("send-button не стал активным за 120 сек (файлы не загрузились?)")

    await btn.click()
    log.info("Submit нажат")


async def _dump_page_state(page: Page, tag: str) -> None:
    """Сохранить screenshot + список <img> при таймауте — для ручной отладки."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = LOGS_DIR / f"timeout_{tag}_{ts}"
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        info = await page.evaluate(
            """() => [...document.querySelectorAll('img')].map(im => ({
                src: (im.src || '').slice(0, 150),
                alt: im.alt,
                w: im.naturalWidth,
                h: im.naturalHeight,
                complete: im.complete,
                visible: im.offsetWidth > 0,
                parentRole: (im.closest('[data-message-author-role], [data-author-role]') || {}).getAttribute
                    ? (im.closest('[data-message-author-role], [data-author-role]')
                        .getAttribute('data-message-author-role')
                        || im.closest('[data-message-author-role], [data-author-role]')
                            .getAttribute('data-author-role'))
                    : null
            }))"""
        )
        base.with_suffix(".json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.error("Дамп сохранён: %s.png + .json", base)
    except Exception as de:
        log.warning("Не удалось сохранить дамп: %s", de)


async def wait_for_generation(
    page: Page, timeout_ms: int, baseline_srcs: list[str]
) -> None:
    log.info(
        "Жду результат до %d сек… (baseline: %d картинок)",
        timeout_ms // 1000, len(baseline_srcs),
    )

    deadline_ms = timeout_ms + 30_000  # +30 сек grace period
    elapsed = 0
    # Передаём baseline через arg page.evaluate(js, arg) — иначе JSON.escape для CSS
    check_js = f"(baseline) => !!({FIND_GENERATED_IMG_JS.strip()})(baseline)"
    while elapsed < deadline_ms:
        try:
            ready = await page.evaluate(check_js, baseline_srcs)
        except Exception as e:
            log.warning("Ошибка JS-проверки готовности: %s", e)
            ready = False
        if ready:
            log.info("Картинка готова, ждём полной загрузки пикселей…")
            await asyncio.sleep(1)
            return
        if elapsed > 0 and elapsed % 30000 == 0:
            log.info("Ещё жду… %d сек", elapsed // 1000)
        await asyncio.sleep(2)
        elapsed += 2000

    await _dump_page_state(page, "wait_gen")
    raise RuntimeError(f"Изображение не появилось за {deadline_ms // 1000} сек")


async def download_via_anchor(
    page: Page, output_path: Path, baseline_srcs: list[str]
) -> None:
    """Качаем картинку через blob URL + временный <a download>, кликнутый Playwright'ом.

    baseline_srcs — список src которые БЫЛИ на странице до submit; их игнорируем,
    качаем только новую.
    """
    log.info("Готовлю blob и якорь для скачивания…")
    # Используем тот же FIND_GENERATED_IMG_JS — гарантия что качаем именно ту,
    # которую посчитали готовой, и не старую "застрявшую" из предыдущего чата.
    prepared = await page.evaluate(
        """async (baseline) => {
            const findImg = """ + FIND_GENERATED_IMG_JS.strip() + """;
            const img = findImg(baseline);
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
        }""",
        baseline_srcs,
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


async def process_one_file(file_path: Path, mode: str = DEFAULT_MODE) -> Path:
    cfg = get_mode(mode)
    if not cfg.is_configured:
        raise RuntimeError(
            f"Режим '{cfg.key}' ({cfg.label}) не настроен: "
            f"project_url={'есть' if cfg.project_url else 'НЕТ'}, "
            f"эталоны={'все на месте' if all(f.exists() for f in cfg.reference_files) else 'НЕ найдены'}"
        )

    output_path = make_output_path()
    chat_url = cfg.project_url or "https://chatgpt.com/"
    log.info(
        "=== Обработка: %s → %s (mode: %s, url: %s) ===",
        file_path.name, output_path.name, cfg.key, chat_url,
    )

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CHROME_CDP_URL)
        try:
            page = await find_or_open_chatgpt(browser)
            await open_new_chat(page, url=chat_url)

            for ref in cfg.reference_files:
                if not ref.exists():
                    raise FileNotFoundError(f"Эталон не найден: {ref}")
                await paste_image(page, ref)

            await paste_image(page, file_path, settle_seconds=5.0)
            await paste_text(page, cfg.prompt)

            await submit(page)

            # Снимаем baseline ПОСЛЕ submit и settle-паузы. После submit ChatGPT
            # перерисовывает загруженные пользователем картинки с новым src
            # (preview-blob → oaiusercontent.com) — если снять baseline ДО, эти
            # новые URLs пройдут как "свежие" и селектор скачает input-фото.
            # 5 сек хватает чтобы все user-картинки получили финальный URL.
            await asyncio.sleep(5)
            baseline_srcs = await snapshot_image_srcs(page)
            log.info("Baseline картинок после submit: %d", len(baseline_srcs))

            await wait_for_generation(page, GENERATION_TIMEOUT_SEC * 1000, baseline_srcs)
            await download_via_anchor(page, output_path, baseline_srcs)
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
