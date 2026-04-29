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
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    MenuButtonCommands,
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
# Режимы (типы товаров)
# ---------------------------------------------------------------------------
# Боту достаточно знать только key → label. Полная конфигурация (project_url,
# эталоны, промпт) живёт на стороне агента в config.py — бот про неё ничего
# не знает. Чтобы добавить новый режим: дописать строку сюда + Mode(...) в
# локальный config.py агента.

MODES_LABELS: dict[str, str] = {
    "ritual":      "🧺 Корзинки",
    "wreath":      "⚜️ Венки",
    "conditioner": "❄️ Кондиционеры",
}
DEFAULT_MODE = "ritual"
MODE_BY_LABEL = {v: k for k, v in MODES_LABELS.items()}

# Какие режимы требуют ввод характеристик пользователем перед генерацией.
# Должен совпадать с requires_specs в локальном config.py агента.
MODES_WITH_SPECS = {"conditioner"}

# Маркер сообщения-приглашения ввести характеристики. Telegram не отдаёт
# нам напрямую "это reply на ForceReply", но мы можем узнать ответ по
# message.reply_to_message.text — сравнивая с этим уникальным заголовком.
SPECS_PROMPT_HEADER = "📝 Характеристики кондиционера"
BTN_SPECS = "📝 Характеристики"


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
                mode            TEXT    NOT NULL DEFAULT 'ritual',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                output_filename   TEXT,
                archived_filename TEXT,
                failed_filename   TEXT,
                error_text      TEXT,
                result_sent     INTEGER DEFAULT 0
            )
        """)
        # Миграции для существующих БД (ALTER TABLE кидает если колонка уже есть)
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN failed_filename TEXT",
            "ALTER TABLE jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'ritual'",
            "ALTER TABLE jobs ADD COLUMN specs TEXT",
        ):
            try:
                conn.execute(ddl)
            except Exception:
                pass
        # Per-user текущий режим + черновик характеристик для следующего фото
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_state (
                chat_id        INTEGER PRIMARY KEY,
                mode           TEXT NOT NULL DEFAULT 'ritual',
                pending_specs  TEXT,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            conn.execute("ALTER TABLE user_state ADD COLUMN pending_specs TEXT")
        except Exception:
            pass
        conn.commit()


def get_user_mode(chat_id: int) -> str:
    """Текущий режим пользователя (default = ritual)."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT mode FROM user_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row and row["mode"] in MODES_LABELS:
        return row["mode"]
    return DEFAULT_MODE


def set_user_mode(chat_id: int, mode: str) -> None:
    if mode not in MODES_LABELS:
        return
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO user_state (chat_id, mode, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at",
            (chat_id, mode, datetime.now().isoformat()),
        )
        conn.commit()


def get_user_specs(chat_id: int) -> str | None:
    """Текущий черновик характеристик пользователя (используется для следующего фото)."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT pending_specs FROM user_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return (row["pending_specs"] if row and row["pending_specs"] else None)


def set_user_specs(chat_id: int, specs: str | None) -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO user_state (chat_id, mode, pending_specs, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "pending_specs=excluded.pending_specs, updated_at=excluded.updated_at",
            (chat_id, DEFAULT_MODE, specs, datetime.now().isoformat()),
        )
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

# Reply-клавиатура: режимы / характеристики+статус / действия.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(MODES_LABELS["ritual"]),
         KeyboardButton(MODES_LABELS["wreath"]),
         KeyboardButton(MODES_LABELS["conditioner"])],
        [KeyboardButton(BTN_SPECS), KeyboardButton(BTN_STATUS)],
        [KeyboardButton(BTN_CLEAR), KeyboardButton(BTN_RESTART)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def _pending_count() -> int:
    with db_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]


def _pending_count_by_mode(mode: str) -> int:
    with db_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='pending' AND mode=?", (mode,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return
    chat_id = update.effective_chat.id
    current_mode = get_user_mode(chat_id)
    modes_list = "\n".join(f"  {label}" for label in MODES_LABELS.values())
    await update.message.reply_text(
        f"Привет! Текущий режим: {MODES_LABELS[current_mode]}\n"
        "Пришли фото — обработаю как карточку для этого режима.\n\n"
        f"Переключение режима — кнопкой:\n{modes_list}\n\n"
        "Действия:\n"
        f"  {BTN_STATUS} — счётчик очереди\n"
        f"  {BTN_CLEAR} — снять все ожидающие\n"
        f"  {BTN_RESTART} — пере-запустить зависшие (>5 мин)",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    current_mode = get_user_mode(chat_id)
    current_specs = get_user_specs(chat_id)
    with db_conn() as conn:
        pending    = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
        processing = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='processing'").fetchone()[0]
        done       = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0]
        cancelled  = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='cancelled'").fetchone()[0]
        # Разбивка очереди по режимам
        by_mode_rows = conn.execute(
            "SELECT mode, COUNT(*) FROM jobs WHERE status='pending' GROUP BY mode"
        ).fetchall()
    by_mode = "\n".join(
        f"  {MODES_LABELS.get(r[0], r[0])}: {r[1]}" for r in by_mode_rows
    ) or "  (пусто)"

    specs_block = ""
    if current_mode in MODES_WITH_SPECS:
        if current_specs:
            preview = current_specs if len(current_specs) <= 200 else current_specs[:200] + "…"
            specs_block = f"\nХарактеристики ({len(current_specs)} симв.):\n{preview}\n"
        else:
            specs_block = "\nХарактеристики не заданы (будет дефолт из промпта)\n"

    await update.message.reply_text(
        f"Ваш режим: {MODES_LABELS[current_mode]}"
        f"{specs_block}\n"
        f"В очереди:       {pending}\n"
        f"  по режимам:\n{by_mode}\n"
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


async def cmd_request_specs(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь нажал «📝 Характеристики» — просим ответить на это сообщение
    списком характеристик. Связь устанавливается через ForceReply: ответ Telegram
    автоматически прикрепит к нему reply_to_message, по тексту которого мы
    распознаем «это специи»."""
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    current = get_user_specs(update.effective_chat.id)
    current_block = (
        f"\n\nСейчас сохранено:\n{current}" if current else
        "\n\n(Пока не задано — будет использован дефолтный список.)"
    )
    await update.message.reply_text(
        f"{SPECS_PROMPT_HEADER}\n\n"
        "Ответьте на это сообщение списком характеристик — каждая на отдельной строке. "
        "Они подставятся в промпт при следующей генерации в режиме «❄️ Кондиционеры»."
        f"{current_block}\n\n"
        "Пример:\n"
        "Инверторный компрессор\n"
        "Класс энергоэффективности A++\n"
        "Уровень шума 22 дБ\n"
        "Площадь до 25 м²",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Список характеристик…"),
    )


async def on_specs_reply(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply на сообщение-приглашение — сохраняем введённые характеристики."""
    msg = update.message
    if not msg or not msg.text:
        return
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    text = msg.text.strip()
    if not text:
        return

    # Команда сброса
    if text.lower() in ("сброс", "reset", "очистить", "clear"):
        set_user_specs(update.effective_chat.id, None)
        await msg.reply_text(
            "🗑 Характеристики сброшены. Будет использован дефолтный список из промпта.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    set_user_specs(update.effective_chat.id, text)
    await msg.reply_text(
        f"✅ Характеристики сохранены ({len(text)} симв.).\n"
        "При следующем фото в режиме «❄️ Кондиционеры» они будут подставлены в промпт.\n\n"
        "Чтобы изменить — снова нажмите «📝 Характеристики».\n"
        "Чтобы сбросить — ответьте на любое сообщение бота словом «сброс».",
        reply_markup=MAIN_KEYBOARD,
    )


async def _route_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Текст-reply на сообщение бота: если это reply на специальный заголовок —
    роутим на on_specs_reply, иначе игнорим (обычный reply пользователя)."""
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    parent_text = msg.reply_to_message.text or ""
    if parent_text.startswith(SPECS_PROMPT_HEADER) or "Характеристики сохранены" in parent_text:
        await on_specs_reply(update, ctx)


async def on_keyboard_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий reply-клавиатуры (приходят как обычные текстовые сообщения)."""
    if not update.message or not update.message.text:
        return
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    # Переключение режима
    if text in MODE_BY_LABEL:
        new_mode = MODE_BY_LABEL[text]
        set_user_mode(chat_id, new_mode)
        await update.message.reply_text(
            f"✅ Режим переключён: {MODES_LABELS[new_mode]}\n"
            "Все следующие фото будут обрабатываться в этом режиме.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # Действия
    if text == BTN_STATUS:
        await cmd_status(update, ctx)
    elif text == BTN_CLEAR:
        await cmd_clear_queue(update, ctx)
    elif text == BTN_RESTART:
        await cmd_restart_stuck(update, ctx)
    elif text == BTN_SPECS:
        await cmd_request_specs(update, ctx)


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
        await msg.reply_text(
            "Пришли мне ФОТО (как картинку или как файл-изображение).",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    filename = f"tg_{ts}{ext}"
    target = INPUT_DIR / filename
    await tg_file.download_to_drive(str(target))
    log.info("Сохранил: %s (%d байт)", filename, target.stat().st_size)

    mode = get_user_mode(msg.chat_id)
    specs = get_user_specs(msg.chat_id) if mode in MODES_WITH_SPECS else None
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs) VALUES (?, ?, ?, ?)",
            (msg.chat_id, filename, mode, specs),
        )
        conn.commit()

    pending = _pending_count()
    mode_label = MODES_LABELS[mode]

    # Подсказка если режиму нужны специи, а пользователь их не задал
    specs_hint = ""
    if mode in MODES_WITH_SPECS and not specs:
        specs_hint = (
            "\n⚠️ Характеристики не заданы — будет использован дефолтный список. "
            "Чтобы задать свои — нажмите «📝 Характеристики»."
        )
    elif specs:
        specs_hint = f"\n📝 С характеристиками ({len(specs)} симв.)"

    if pending == 1:
        await msg.reply_text(
            f"✅ Принял ({mode_label}). Обрабатываю прямо сейчас (~1–2 мин).{specs_hint}",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await msg.reply_text(
            f"✅ Принял ({mode_label}). В очереди: {pending}.{specs_hint}",
            reply_markup=MAIN_KEYBOARD,
        )


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
        await q.message.reply_text(f"Неизвестный callback: {data}", reply_markup=MAIN_KEYBOARD)
        return

    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        await q.message.reply_text(f"Задача #{job_id} не найдена в БД.", reply_markup=MAIN_KEYBOARD)
        return

    if action == "redo":
        archived_name = row["archived_filename"]
        if not archived_name:
            await q.message.reply_text("⚠️ У этой задачи нет исходника в processed/.", reply_markup=MAIN_KEYBOARD)
            return
        src = PROCESSED_DIR / archived_name
        if not src.exists():
            await q.message.reply_text(
                f"⚠️ Исходник «{archived_name}» не найден в processed/. Пришли фото заново."
            , reply_markup=MAIN_KEYBOARD)
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
            f"♻️ Поставил на повторную генерацию. В очереди: {_pending_count()}.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if action == "retry":
        failed_name = row["failed_filename"]
        if not failed_name:
            await q.message.reply_text("⚠️ У этой задачи нет исходника в failed/.", reply_markup=MAIN_KEYBOARD)
            return
        src = FAILED_DIR / failed_name
        if not src.exists():
            await q.message.reply_text(
                f"⚠️ Файл «{failed_name}» не найден в failed/. Пришли фото заново."
            , reply_markup=MAIN_KEYBOARD)
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
            f"🔄 Поставил на повтор. В очереди: {_pending_count()}.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if action == "bad":
        bad_name = row["output_filename"]
        if not bad_name:
            await q.message.reply_text("⚠️ У этой задачи нет output_filename.", reply_markup=MAIN_KEYBOARD)
            return
        src = OUTPUT_DIR / bad_name
        if not src.exists():
            await q.message.reply_text(f"Файл «{bad_name}» уже не в output/.", reply_markup=MAIN_KEYBOARD)
            return
        bad_dir = OUTPUT_DIR.parent / "bad_results"
        bad_dir.mkdir(exist_ok=True)
        try:
            src.rename(bad_dir / bad_name)
            await q.message.reply_text(f"🗑 Перенёс в bad_results/: {bad_name}", reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            await q.message.reply_text(f"Не получилось перенести: {e}", reply_markup=MAIN_KEYBOARD)


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
                    job_mode = row["mode"] if "mode" in row.keys() else DEFAULT_MODE
                    mode_label = MODES_LABELS.get(job_mode, job_mode)
                    caption = (
                        f"✅ Готово ({mode_label}): {row['output_filename']}\n"
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

    # Slash-меню (синяя кнопка слева от поля ввода) — список доступных команд.
    # Telegram-клиент будет показывать это меню всегда, пока бот не сменит.
    await app.bot.set_my_commands([
        BotCommand("start",         "Главное меню и режимы"),
        BotCommand("status",        "Очередь, режим, характеристики"),
        BotCommand("specs",         "Ввести характеристики (для кондиционеров)"),
        BotCommand("clear",         "Снять все ожидающие задачи"),
        BotCommand("restart_stuck", "Пере-запустить зависшие (>5 мин)"),
        BotCommand("help",          "Справка"),
    ])
    # Кнопка «Menu» (рядом с полем ввода) — открывает список команд выше.
    # Без этого вызова Telegram показывает дефолтную кнопку «/», которая для
    # некоторых клиентов выглядит менее заметно.
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

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
    app.add_handler(CommandHandler("specs", cmd_request_specs))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo))

    # Ответ на ForceReply-приглашение «📝 Характеристики кондиционера…».
    # Должен идти РАНЬШЕ чем on_keyboard_text — иначе текст попадёт в общий хендлер.
    app.add_handler(MessageHandler(
        filters.TEXT & filters.REPLY,
        _route_reply,
    ))

    # Reply-клавиатура шлёт обычный текст — ловим точные совпадения с подписями кнопок
    import re
    button_labels = list(MODES_LABELS.values()) + [BTN_SPECS, BTN_STATUS, BTN_CLEAR, BTN_RESTART]
    pattern = "^(" + "|".join(re.escape(b) for b in button_labels) + ")$"
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(pattern),
        on_keyboard_text,
    ))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("VPS-бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
