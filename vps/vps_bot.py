"""VPS-бот: принимает фото в Telegram, кладёт в SQLite-очередь,
отправляет готовые результаты обратно пользователю.

Запуск: python vps_bot.py
"""
from __future__ import annotations

import os

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import asyncio
import logging
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config_vps import (
    DB_PATH,
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
log = logging.getLogger("vps_bot")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id         INTEGER NOT NULL,
                input_filename  TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                output_filename   TEXT,
                archived_filename TEXT,
                failed_filename   TEXT,
                error_text      TEXT,
                result_sent     INTEGER DEFAULT 0
            )
        """)
        # Миграция: добавляем failed_filename если его нет (для существующих БД)
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN failed_filename TEXT")
        except Exception:
            pass
        conn.commit()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _allowed(user_id: int | None) -> bool:
    if not TELEGRAM_ALLOWED_USER_ID:
        return True
    return user_id == TELEGRAM_ALLOWED_USER_ID


# ---------------------------------------------------------------------------
# Reply-клавиатура: 3 кнопки быстрого доступа, всегда внизу экрана
# ---------------------------------------------------------------------------

BTN_STATUS  = "📊 Статус"
BTN_CLEAR   = "❌ Очистить очередь"
BTN_RESTART = "♻️ Рестарт зависших"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_STATUS)],
        [KeyboardButton(BTN_CLEAR), KeyboardButton(BTN_RESTART)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def _pending_count() -> int:
    with db_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return
    await update.message.reply_text(
        "Привет! Пришли мне фото товара (как фото или документ) — обработаю в карточку.\n\n"
        "Кнопки внизу экрана:\n"
        f"  {BTN_STATUS} — счётчик очереди\n"
        f"  {BTN_CLEAR} — снять все ожидающие задачи\n"
        f"  {BTN_RESTART} — пере-запустить зависшие (processing >5 мин)",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    with db_conn() as conn:
        pending    = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
        processing = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='processing'").fetchone()[0]
        done       = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0]
        cancelled  = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='cancelled'").fetchone()[0]
    await update.message.reply_text(
        f"В очереди:       {pending}\n"
        f"Обрабатывается:  {processing}\n"
        f"Готово всего:    {done}\n"
        f"Отменено:        {cancelled}",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_clear_queue(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Снимаем все pending задачи (помечаем cancelled). Текущую processing
    не трогаем — она доживёт до конца, а её результат всё равно ждут."""
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    now_iso = datetime.now().isoformat()
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='cancelled', updated_at=? WHERE status='pending'",
            (now_iso,),
        )
        n = cur.rowcount
        conn.commit()
    await update.message.reply_text(
        f"❌ Очередь очищена. Снято: {n}.\n"
        "(Если что-то сейчас обрабатывается — оно доживёт до конца.)",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_restart_stuck(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Возвращаем processing-задачи старше 5 мин обратно в pending —
    агент подхватит их при следующем опросе."""
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    now = datetime.now()
    cutoff = (now - timedelta(minutes=5)).isoformat()
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='pending', updated_at=? "
            "WHERE status='processing' AND updated_at < ?",
            (now.isoformat(), cutoff),
        )
        n = cur.rowcount
        conn.commit()
    await update.message.reply_text(
        f"♻️ Сброшено зависших: {n}.\n"
        f"В очереди сейчас: {_pending_count()}.",
        reply_markup=MAIN_KEYBOARD,
    )


async def on_keyboard_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий reply-клавиатуры (приходят как обычные текстовые сообщения)."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text == BTN_STATUS:
        await cmd_status(update, ctx)
    elif text == BTN_CLEAR:
        await cmd_clear_queue(update, ctx)
    elif text == BTN_RESTART:
        await cmd_restart_stuck(update, ctx)


async def handle_photo(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not update.effective_user or not _allowed(update.effective_user.id):
        if msg:
            await msg.reply_text("Доступ запрещён.")
        return

    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        ext = ".jpg"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        tg_file = await msg.document.get_file()
        ext = Path(msg.document.file_name or "input.jpg").suffix or ".jpg"
    else:
        await msg.reply_text("Пришли мне ФОТО (как картинку или как файл-изображение).")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    filename = f"tg_{ts}{ext}"
    target = INPUT_DIR / filename
    await tg_file.download_to_drive(str(target))
    log.info("Сохранил: %s (%d байт)", filename, target.stat().st_size)

    with db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (chat_id, input_filename) VALUES (?, ?)",
            (msg.chat_id, filename),
        )
        conn.commit()

    with db_conn() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]

    if pending == 1:
        await msg.reply_text("✅ Принял. Обрабатываю прямо сейчас (~1–2 мин).")
    else:
        await msg.reply_text(f"✅ Принял. В очереди: {pending}.")


async def on_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not update.effective_user or not _allowed(update.effective_user.id):
        if q:
            await q.answer("Доступ запрещён.")
        return

    data = q.data or ""
    await q.answer()

    # callback_data компактный: "<action>:<job_id>" — гарантированно <64 байт.
    # Имена файлов читаем из БД по job_id (Telegram limit на callback_data = 64).
    try:
        action, _, job_id_str = data.partition(":")
        job_id = int(job_id_str)
    except ValueError:
        await q.message.reply_text(f"Неизвестный callback: {data}")
        return

    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        await q.message.reply_text(f"Задача #{job_id} не найдена в БД.")
        return

    if action == "redo":
        archived_name = row["archived_filename"]
        if not archived_name:
            await q.message.reply_text("⚠️ У этой задачи нет исходника в processed/.")
            return
        src = PROCESSED_DIR / archived_name
        if not src.exists():
            await q.message.reply_text(
                f"⚠️ Исходник «{archived_name}» не найден в processed/. Пришли фото заново."
            )
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        new_filename = f"redo_{ts}_{archived_name}"
        shutil.copyfile(src, INPUT_DIR / new_filename)
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO jobs (chat_id, input_filename) VALUES (?, ?)",
                (q.message.chat_id, new_filename),
            )
            conn.commit()
        await q.message.reply_text(
            f"♻️ Поставил на повторную генерацию. В очереди: {_pending_count()}."
        )
        return

    if action == "retry":
        failed_name = row["failed_filename"]
        if not failed_name:
            await q.message.reply_text("⚠️ У этой задачи нет исходника в failed/.")
            return
        src = FAILED_DIR / failed_name
        if not src.exists():
            await q.message.reply_text(
                f"⚠️ Файл «{failed_name}» не найден в failed/. Пришли фото заново."
            )
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        new_filename = f"retry_{ts}_{failed_name}"
        shutil.copyfile(src, INPUT_DIR / new_filename)
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO jobs (chat_id, input_filename) VALUES (?, ?)",
                (q.message.chat_id, new_filename),
            )
            conn.commit()
        await q.message.reply_text(
            f"🔄 Поставил на повтор. В очереди: {_pending_count()}."
        )
        return

    if action == "bad":
        bad_name = row["output_filename"]
        if not bad_name:
            await q.message.reply_text("⚠️ У этой задачи нет output_filename.")
            return
        src = OUTPUT_DIR / bad_name
        if not src.exists():
            await q.message.reply_text(f"Файл «{bad_name}» уже не в output/.")
            return
        bad_dir = OUTPUT_DIR.parent / "bad_results"
        bad_dir.mkdir(exist_ok=True)
        try:
            src.rename(bad_dir / bad_name)
            await q.message.reply_text(f"🗑 Перенёс в bad_results/: {bad_name}")
        except Exception as e:
            await q.message.reply_text(f"Не получилось перенести: {e}")


# ---------------------------------------------------------------------------
# Background task: отправляем готовые и уведомляем об ошибках
# ---------------------------------------------------------------------------

async def result_sender(app: Application) -> None:
    log.info("Result sender запущен")
    while True:
        await asyncio.sleep(5)
        try:
            with db_conn() as conn:
                done_rows   = conn.execute(
                    "SELECT * FROM jobs WHERE status='done'   AND result_sent=0 ORDER BY id"
                ).fetchall()
                failed_rows = conn.execute(
                    "SELECT * FROM jobs WHERE status='failed' AND result_sent=0 ORDER BY id"
                ).fetchall()

            for row in done_rows:
                try:
                    out_path = OUTPUT_DIR / row["output_filename"]
                    if not out_path.exists():
                        log.warning("Файл результата пропал: %s", row["output_filename"])
                        with db_conn() as conn:
                            conn.execute("UPDATE jobs SET result_sent=1 WHERE id=?", (row["id"],))
                            conn.commit()
                        continue

                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "🔄 Перегенерировать",
                            callback_data=f"redo:{row['id']}",
                        )],
                        [InlineKeyboardButton(
                            "🗑 Удалить (плохой)",
                            callback_data=f"bad:{row['id']}",
                        )],
                    ])
                    pending_now = _pending_count()
                    caption = (
                        f"✅ Готово: {row['output_filename']}\n"
                        f"В очереди: {pending_now}"
                    )
                    with open(out_path, "rb") as f:
                        await app.bot.send_document(
                            chat_id=row["chat_id"],
                            document=InputFile(f, filename=row["output_filename"]),
                            caption=caption,
                            reply_markup=keyboard,
                        )
                    with db_conn() as conn:
                        conn.execute("UPDATE jobs SET result_sent=1 WHERE id=?", (row["id"],))
                        conn.commit()
                    log.info("Отправлено: %s → chat %s", row["output_filename"], row["chat_id"])
                except Exception as e:
                    log.exception("Ошибка отправки job %s: %s", row["id"], e)

            for row in failed_rows:
                try:
                    # Кнопка «Повторить» если есть файл в failed/
                    keyboard = None
                    if row["failed_filename"]:
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "🔄 Повторить",
                                callback_data=f"retry:{row['id']}",
                            )
                        ]])
                    await app.bot.send_message(
                        chat_id=row["chat_id"],
                        text=(
                            f"❌ Ошибка при обработке (3 попытки):\n"
                            f"{row['error_text']}\n\n"
                            "Нажми «Повторить» или пришли фото заново."
                        ),
                        reply_markup=keyboard,
                    )
                    with db_conn() as conn:
                        conn.execute("UPDATE jobs SET result_sent=1 WHERE id=?", (row["id"],))
                        conn.commit()
                except Exception as e:
                    log.exception("Ошибка уведомления о сбое job %s: %s", row["id"], e)

        except Exception as e:
            log.exception("result_sender упал: %s", e)


async def post_init(app: Application) -> None:
    init_db()
    # asyncio.create_task работает корректно, т.к. post_init вызывается внутри event loop
    import asyncio as _asyncio
    _asyncio.get_event_loop().create_task(result_sender(app))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("clear", cmd_clear_queue))
    app.add_handler(CommandHandler("restart_stuck", cmd_restart_stuck))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))
    # Reply-клавиатура шлёт обычный текст — ловим точные совпадения с подписями кнопок
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(f"^({BTN_STATUS}|{BTN_CLEAR}|{BTN_RESTART})$"),
        on_keyboard_text,
    ))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("VPS-бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
