"""Telegram-бот: принимает фото, кладёт в input/, обрабатывает по очереди,
отправляет результат обратно в чат и складывает в output/.

Запуск: python bot.py
"""
from __future__ import annotations

import os

# Отключаем системный HTTP_PROXY/HTTPS_PROXY — api.telegram.org ходит напрямую,
# а через прокси httpx таймаутит. Делать ДО импорта httpx/telegram.
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import archive_input, process_one_file
from config import (
    DELAY_BETWEEN_JOBS_SEC,
    FAILED_DIR,
    INPUT_DIR,
    LOGS_DIR,
    OUTPUT_DIR,
    PROCESSED_DIR,
    TELEGRAM_ALLOWED_USER_ID,
    TELEGRAM_BOT_TOKEN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


@dataclass
class Job:
    chat_id: int
    file_path: Path
    received_at: datetime


JOB_QUEUE: asyncio.Queue[Job] = asyncio.Queue()


def _allowed(user_id: int | None) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True  # если не настроен — пускаем всех (DEV)
    return user_id == TELEGRAM_ALLOWED_USER_ID


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return
    await update.message.reply_text(
        "Привет! Пришли мне фото товара (как фото или документ) — обработаю в карточку.\n\n"
        "Команды:\n"
        "/status — состояние очереди\n"
        "/help — это сообщение"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    pending = JOB_QUEUE.qsize()
    pending_files = len(list(INPUT_DIR.glob("*")))
    await update.message.reply_text(
        f"В очереди: {pending} задач\n"
        f"Файлов в input/: {pending_files}\n"
        f"Готовых в output/: {len(list(OUTPUT_DIR.glob('*.png')))}"
    )


async def handle_photo(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not update.effective_user or not _allowed(update.effective_user.id):
        if msg:
            await msg.reply_text("Доступ запрещён.")
        return

    # Получаем файл (из photo или document)
    if msg.photo:
        # Берём самое большое разрешение
        tg_file = await msg.photo[-1].get_file()
        ext = ".jpg"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        tg_file = await msg.document.get_file()
        ext = Path(msg.document.file_name or "input.jpg").suffix or ".jpg"
    else:
        await msg.reply_text("Пришли мне ФОТО (как картинку или как файл-изображение).")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    target = INPUT_DIR / f"tg_{ts}{ext}"
    await tg_file.download_to_drive(str(target))
    log.info("Сохранил: %s (%d байт)", target.name, target.stat().st_size)

    job = Job(chat_id=msg.chat_id, file_path=target, received_at=datetime.now())
    await JOB_QUEUE.put(job)

    pending = JOB_QUEUE.qsize()
    if pending == 1:
        await msg.reply_text("✅ Принял. Обрабатываю прямо сейчас (~1–2 мин).")
    else:
        await msg.reply_text(f"✅ Принял. В очереди передо мной: {pending - 1}.")


def _move_to_failed(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = FAILED_DIR / f"{ts}_{path.name}"
    try:
        path.rename(target)
    except Exception:
        return path
    return target


async def worker(app: Application) -> None:
    """Фоновый worker: обрабатывает Job'ы из очереди по одному."""
    log.info("Worker запущен")
    batch_done = 0
    while True:
        job = await JOB_QUEUE.get()
        position = JOB_QUEUE.qsize()  # сколько ещё после этой
        try:
            log.info("Старт обработки: %s (после: %d)", job.file_path.name, position)
            queue_note = f" (ещё в очереди: {position})" if position else ""
            await app.bot.send_message(
                chat_id=job.chat_id,
                text=f"⏳ Начал обработку «{job.file_path.name}»…{queue_note}",
            )
            output_path = await process_one_file(job.file_path)
            archived = archive_input(job.file_path)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"redo:{archived.name}")],
                [InlineKeyboardButton("🗑 Удалить (плохой)", callback_data=f"bad:{output_path.name}")],
            ])
            with open(output_path, "rb") as f:
                await app.bot.send_document(
                    chat_id=job.chat_id,
                    document=InputFile(f, filename=output_path.name),
                    caption=f"✅ Готово: {output_path.name}",
                    reply_markup=keyboard,
                )
            log.info("Готово: %s", output_path.name)
            batch_done += 1
        except Exception as e:
            log.exception("Ошибка обработки %s: %s", job.file_path, e)
            failed_path = _move_to_failed(job.file_path)
            try:
                await app.bot.send_message(
                    chat_id=job.chat_id,
                    text=(
                        f"❌ Ошибка на «{job.file_path.name}»:\n{e}\n\n"
                        f"Исходник перемещён в failed/ — попробуй позже или другое фото."
                    ),
                )
            except Exception:
                pass
        finally:
            JOB_QUEUE.task_done()

        # Если очередь пуста и за этот пакет было больше 1 — отправим сводку
        if JOB_QUEUE.empty() and batch_done > 1:
            try:
                await app.bot.send_message(
                    chat_id=job.chat_id,
                    text=f"🎉 Все задачи готовы. Обработано: {batch_done}.",
                )
            except Exception:
                pass
            batch_done = 0
        elif JOB_QUEUE.empty():
            batch_done = 0
        else:
            # Пауза между задачами, чтобы не нарваться на rate-limit ChatGPT
            await asyncio.sleep(DELAY_BETWEEN_JOBS_SEC)


async def on_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not update.effective_user or not _allowed(update.effective_user.id):
        if q:
            await q.answer("Доступ запрещён.")
        return

    data = q.data or ""
    await q.answer()

    if data.startswith("redo:"):
        archived_name = data[len("redo:"):]
        src = PROCESSED_DIR / archived_name
        if not src.exists():
            await q.message.reply_text(
                f"⚠️ Исходник «{archived_name}» не найден в processed/. "
                f"Видимо, его уже удалили — пришли фото заново."
            )
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        new_input = INPUT_DIR / f"redo_{ts}_{archived_name}"
        shutil.copyfile(src, new_input)

        job = Job(chat_id=q.message.chat_id, file_path=new_input, received_at=datetime.now())
        await JOB_QUEUE.put(job)
        pending = JOB_QUEUE.qsize()
        await q.message.reply_text(
            f"♻️ Поставил на повторную генерацию. В очереди передо мной: {pending - 1}."
        )
        return

    if data.startswith("bad:"):
        bad_name = data[len("bad:"):]
        src = OUTPUT_DIR / bad_name
        if not src.exists():
            await q.message.reply_text(f"Файл «{bad_name}» уже не в output/ — удалять нечего.")
            return
        bad_dir = OUTPUT_DIR.parent / "bad_results"
        bad_dir.mkdir(exist_ok=True)
        target = bad_dir / bad_name
        try:
            src.rename(target)
            await q.message.reply_text(f"🗑 Перенёс в bad_results/: {bad_name}")
        except Exception as e:
            await q.message.reply_text(f"Не получилось перенести: {e}")


async def post_init(app: Application) -> None:
    app.create_task(worker(app))


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не задан. Скопируй .env.example в .env и заполни."
        )

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Бот запущен. Отправляй фото в Telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
