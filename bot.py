"""Telegram-бот: фото → выбор режима → (характеристики) → генерация → результат в чат + канал + GDrive."""
from __future__ import annotations

import os

# Отключаем системный прокси ДО импорта httpx/telegram
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
    GDRIVE_CREDENTIALS_JSON,
    GDRIVE_FOLDER_ID,
    INPUT_DIR,
    LOGS_DIR,
    MODES,
    OUTPUT_DIR,
    PROCESSED_DIR,
    TELEGRAM_ALLOWED_USER_ID,
    TELEGRAM_BOT_TOKEN,
    get_mode,
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

_job_counter = 0


@dataclass
class Job:
    job_id: int
    chat_id: int
    file_path: Path
    mode: str
    specs: str | None
    received_at: datetime


# Очередь задач + зеркальный список для /cancel и /status
JOB_QUEUE: asyncio.Queue[Job] = asyncio.Queue()
ENQUEUED: list[Job] = []          # задачи в очереди (ещё не взяты воркером)
ENQUEUED_LOCK = asyncio.Lock()
CANCELLED_IDS: set[int] = set()   # job_id, помеченные для отмены

# Состояние диалога с пользователем (ожидание выбора режима / ввода характеристик)
# {user_id: {"step": "wait_mode"|"wait_specs", "file_path": Path, "mode": str|None}}
USER_STATE: dict[int, dict] = {}


def _allowed(user_id: int | None) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True
    return user_id == TELEGRAM_ALLOWED_USER_ID


def _mode_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора режима из всех включённых Mode."""
    row: list[InlineKeyboardButton] = []
    buttons: list[list[InlineKeyboardButton]] = []
    for mode in MODES.values():
        if not mode.enabled:
            continue
        row.append(InlineKeyboardButton(mode.label, callback_data=f"mode:{mode.key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_input")])
    return InlineKeyboardMarkup(buttons)


def _clear_user_state(user_id: int) -> None:
    state = USER_STATE.pop(user_id, None)
    if state and state.get("file_path"):
        try:
            state["file_path"].unlink(missing_ok=True)
        except Exception:
            pass


# ── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return
    await update.message.reply_text(
        "Привет! Пришли фото товара (как картинку или файл) — выбери режим и обработаю.\n\n"
        "/status — состояние очереди\n"
        "/cancel — отменить задачи из очереди\n"
        "/help — справка"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    async with ENQUEUED_LOCK:
        pending = [j for j in ENQUEUED if j.job_id not in CANCELLED_IDS]
    lines = [
        f"В очереди: {len(pending)}",
        f"Готово в output/: {len(list(OUTPUT_DIR.glob('*.png')))}",
    ]
    for i, j in enumerate(pending, 1):
        lines.append(f"  {i}. #{j.job_id} [{get_mode(j.mode).label}] {j.file_path.name}")
    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    async with ENQUEUED_LOCK:
        pending = [j for j in ENQUEUED if j.job_id not in CANCELLED_IDS]
    if not pending:
        await update.message.reply_text("Очередь пуста — нечего отменять.")
        return
    buttons = [
        [InlineKeyboardButton(
            f"❌ #{j.job_id} [{get_mode(j.mode).label}] {j.file_path.name}",
            callback_data=f"cancelq:{j.job_id}",
        )]
        for j in pending
    ]
    buttons.append([InlineKeyboardButton("🗑 Отменить ВСЕ", callback_data="cancelq:all")])
    await update.message.reply_text(
        "Выбери задачу для отмены:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Приём фото ───────────────────────────────────────────────────────────────

async def handle_photo(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not update.effective_user or not _allowed(update.effective_user.id):
        if msg:
            await msg.reply_text("Доступ запрещён.")
        return

    user_id = update.effective_user.id
    _clear_user_state(user_id)  # сбросить незавершённый предыдущий диалог

    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        ext = ".jpg"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        tg_file = await msg.document.get_file()
        ext = Path(msg.document.file_name or "input.jpg").suffix or ".jpg"
    else:
        await msg.reply_text("Пришли ФОТО (как картинку или файл-изображение).")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    target = INPUT_DIR / f"tg_{ts}{ext}"
    await tg_file.download_to_drive(str(target))
    log.info("Сохранил: %s (%d байт)", target.name, target.stat().st_size)

    USER_STATE[user_id] = {"step": "wait_mode", "file_path": target, "mode": None}
    await msg.reply_text("Фото принял. Выбери режим обработки:", reply_markup=_mode_keyboard())


# ── Приём текста (ввод характеристик) ────────────────────────────────────────

async def handle_text(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not update.effective_user or not _allowed(update.effective_user.id):
        return
    user_id = update.effective_user.id
    state = USER_STATE.get(user_id)
    if not state or state["step"] != "wait_specs":
        return  # не ждём текст — игнорируем
    specs = (msg.text or "").strip()
    await _enqueue(msg, user_id, state["file_path"], state["mode"], specs or None)


# ── Постановка в очередь ──────────────────────────────────────────────────────

async def _enqueue(msg, user_id: int, file_path: Path, mode_key: str, specs: str | None) -> None:
    global _job_counter
    _job_counter += 1
    job = Job(
        job_id=_job_counter,
        chat_id=msg.chat_id,
        file_path=file_path,
        mode=mode_key,
        specs=specs,
        received_at=datetime.now(),
    )
    async with ENQUEUED_LOCK:
        ENQUEUED.append(job)
    await JOB_QUEUE.put(job)
    USER_STATE.pop(user_id, None)

    async with ENQUEUED_LOCK:
        queue_pos = sum(1 for j in ENQUEUED if j.job_id not in CANCELLED_IDS) - 1
    label = get_mode(mode_key).label
    if queue_pos <= 0:
        await msg.reply_text(f"✅ Принял [{label}]. Обрабатываю сейчас (~1–2 мин).")
    else:
        await msg.reply_text(f"✅ Принял [{label}]. В очереди перед вами: {queue_pos}.")


# ── Колбэки ───────────────────────────────────────────────────────────────────

async def on_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not update.effective_user or not _allowed(update.effective_user.id):
        if q:
            await q.answer("Доступ запрещён.")
        return

    data = q.data or ""
    await q.answer()
    user_id = update.effective_user.id

    # Выбор режима
    if data.startswith("mode:"):
        mode_key = data[len("mode:"):]
        state = USER_STATE.get(user_id)
        if not state or state["step"] != "wait_mode":
            await q.message.reply_text("⚠️ Нет активного фото. Пришли фото заново.")
            return
        mode = get_mode(mode_key)
        if not mode.is_configured:
            await q.message.reply_text(
                f"⚠️ Режим «{mode.label}» ещё не настроен (нет URL проекта или эталонов).\n"
                "Выбери другой:",
                reply_markup=_mode_keyboard(),
            )
            return
        state["mode"] = mode_key
        if mode.requires_specs:
            state["step"] = "wait_specs"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к выбору режима", callback_data="back_to_mode")],
                [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_specs")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_input")],
            ])
            await q.message.reply_text(
                f"Режим: {mode.label}\n\n"
                "Введи характеристики товара:\n"
                "  Строка 1: Производитель\n"
                "  Строка 2: Модель\n"
                "  Далее: технические характеристики\n\n"
                "Или нажми «Пропустить»:",
                reply_markup=kb,
            )
        else:
            await _enqueue(q.message, user_id, state["file_path"], mode_key, None)
        return

    # Назад к выбору режима
    if data == "back_to_mode":
        state = USER_STATE.get(user_id)
        if not state:
            await q.message.reply_text("⚠️ Нет активного фото. Пришли фото заново.")
            return
        state["step"] = "wait_mode"
        state["mode"] = None
        await q.message.reply_text("Выбери режим:", reply_markup=_mode_keyboard())
        return

    # Пропустить ввод характеристик
    if data == "skip_specs":
        state = USER_STATE.get(user_id)
        if not state or state["step"] != "wait_specs":
            return
        await _enqueue(q.message, user_id, state["file_path"], state["mode"], None)
        return

    # Отмена до постановки в очередь
    if data == "cancel_input":
        _clear_user_state(user_id)
        await q.message.reply_text("❌ Отменено. Пришли фото когда будешь готов.")
        return

    # Отмена задачи из очереди
    if data.startswith("cancelq:"):
        target = data[len("cancelq:"):]
        if target == "all":
            async with ENQUEUED_LOCK:
                to_cancel = [j for j in ENQUEUED if j.job_id not in CANCELLED_IDS]
                for j in to_cancel:
                    CANCELLED_IDS.add(j.job_id)
            await q.message.reply_text(f"❌ Отменено задач: {len(to_cancel)}.")
        else:
            try:
                job_id = int(target)
            except ValueError:
                return
            async with ENQUEUED_LOCK:
                found = any(j.job_id == job_id and j.job_id not in CANCELLED_IDS for j in ENQUEUED)
                if found:
                    CANCELLED_IDS.add(job_id)
            if found:
                await q.message.reply_text(f"❌ Задача #{job_id} отменена.")
            else:
                await q.message.reply_text("Задача уже обработана или не найдена.")
        return

    # Перегенерировать: формат "redo:{mode_key}:{archived_name}"
    if data.startswith("redo:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            _, mode_key, archived_name = parts
        else:
            # Устаревший формат без mode_key
            archived_name = parts[1] if len(parts) > 1 else ""
            mode_key = "ritual"
        src = PROCESSED_DIR / archived_name
        if not src.exists():
            await q.message.reply_text("⚠️ Исходник не найден в processed/ — пришли фото заново.")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        new_input = INPUT_DIR / f"redo_{ts}_{archived_name}"
        shutil.copyfile(src, new_input)
        await _enqueue(q.message, user_id, new_input, mode_key, None)
        return

    # Плохой результат
    if data.startswith("bad:"):
        bad_name = data[len("bad:"):]
        src = OUTPUT_DIR / bad_name
        if not src.exists():
            await q.message.reply_text("Файл уже не в output/.")
            return
        bad_dir = OUTPUT_DIR.parent / "bad_results"
        bad_dir.mkdir(exist_ok=True)
        try:
            src.rename(bad_dir / bad_name)
            await q.message.reply_text(f"🗑 Перенёс в bad_results/: {bad_name}")
        except Exception as e:
            await q.message.reply_text(f"Ошибка: {e}")
        return


# ── Worker ────────────────────────────────────────────────────────────────────

def _move_to_failed(path: Path) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = FAILED_DIR / f"{ts}_{path.name}"
    try:
        path.rename(target)
    except Exception:
        pass


def _parse_brand_model(specs: str | None) -> tuple[str | None, str | None]:
    """Первая строка specs → производитель (brand), вторая → модель (model)."""
    if not specs:
        return None, None
    lines = [ln.strip() for ln in specs.splitlines() if ln.strip()]
    return (lines[0] if lines else None), (lines[1] if len(lines) >= 2 else None)


async def worker(app: Application) -> None:
    log.info("Worker запущен")
    batch_done = 0
    last_chat_id: int | None = None

    while True:
        job = await JOB_QUEUE.get()

        async with ENQUEUED_LOCK:
            try:
                ENQUEUED.remove(job)
            except ValueError:
                pass

        # Пропустить отменённую задачу
        if job.job_id in CANCELLED_IDS:
            CANCELLED_IDS.discard(job.job_id)
            log.info("Пропускаю отменённую задачу #%d", job.job_id)
            try:
                await app.bot.send_message(
                    chat_id=job.chat_id,
                    text=f"⏭ Задача #{job.job_id} ({job.file_path.name}) отменена — пропускаю.",
                )
            except Exception:
                pass
            JOB_QUEUE.task_done()
            continue

        last_chat_id = job.chat_id
        mode = get_mode(job.mode)

        async with ENQUEUED_LOCK:
            remaining = sum(1 for j in ENQUEUED if j.job_id not in CANCELLED_IDS)
        queue_note = f" (ещё в очереди: {remaining})" if remaining else ""

        try:
            log.info("Старт #%d: %s [%s]", job.job_id, job.file_path.name, mode.label)
            await app.bot.send_message(
                chat_id=job.chat_id,
                text=f"⏳ Обрабатываю #{job.job_id} «{job.file_path.name}» [{mode.label}]…{queue_note}",
            )

            brand, model_name = _parse_brand_model(job.specs)
            output_path = await process_one_file(
                job.file_path,
                mode=job.mode,
                specs=job.specs,
                brand=brand,
                model=model_name,
            )
            archived = archive_input(job.file_path)

            # Отправляем пользователю
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔄 Перегенерировать",
                    callback_data=f"redo:{job.mode}:{archived.name}",
                )],
                [InlineKeyboardButton(
                    "🗑 Удалить (плохой)",
                    callback_data=f"bad:{output_path.name}",
                )],
            ])
            with open(output_path, "rb") as f:
                await app.bot.send_document(
                    chat_id=job.chat_id,
                    document=InputFile(f, filename=output_path.name),
                    caption=f"✅ #{job.job_id} [{mode.label}]: {output_path.name}",
                    reply_markup=keyboard,
                )

            # Пересылаем в канал режима
            if mode.telegram_channel_id:
                try:
                    with open(output_path, "rb") as f:
                        await app.bot.send_document(
                            chat_id=int(mode.telegram_channel_id),
                            document=InputFile(f, filename=output_path.name),
                            caption=output_path.name,
                        )
                except Exception as e:
                    log.warning("Ошибка отправки в канал %s: %s", mode.telegram_channel_id, e)

            # Загружаем на Google Drive
            if GDRIVE_CREDENTIALS_JSON:
                folder_id = mode.gdrive_folder_id or GDRIVE_FOLDER_ID
                if folder_id:
                    try:
                        from gdrive import upload_file as _gdrive_upload
                        loop = asyncio.get_running_loop()
                        link = await loop.run_in_executor(
                            None, _gdrive_upload, output_path, folder_id, GDRIVE_CREDENTIALS_JSON
                        )
                        log.info("GDrive: %s", link)
                    except Exception as e:
                        log.warning("GDrive upload failed: %s", e)

            log.info("Готово: %s", output_path.name)
            batch_done += 1

        except Exception as e:
            log.exception("Ошибка #%d %s: %s", job.job_id, job.file_path, e)
            _move_to_failed(job.file_path)
            try:
                await app.bot.send_message(
                    chat_id=job.chat_id,
                    text=(
                        f"❌ Ошибка на #{job.job_id} «{job.file_path.name}»:\n{e}\n\n"
                        "Исходник перемещён в failed/ — попробуй позже или другое фото."
                    ),
                )
            except Exception:
                pass
        finally:
            JOB_QUEUE.task_done()

        # Итоговое сообщение после завершения всей очереди
        async with ENQUEUED_LOCK:
            remaining_after = sum(1 for j in ENQUEUED if j.job_id not in CANCELLED_IDS)
        if remaining_after == 0:
            if batch_done > 1:
                try:
                    await app.bot.send_message(
                        chat_id=last_chat_id,
                        text=f"🎉 Все задачи готовы. Обработано: {batch_done}.",
                    )
                except Exception:
                    pass
            batch_done = 0
        else:
            await asyncio.sleep(DELAY_BETWEEN_JOBS_SEC)


async def post_init(app: Application) -> None:
    app.create_task(worker(app))


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Заполни .env.")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
