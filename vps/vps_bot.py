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
import time as _time
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

import os as _os

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

# Telegram-каналы для пересылки результатов по режиму (берём из .env VPS)
MODES_CHANNELS: dict[str, str] = {
    "ritual":      _os.getenv("RITUAL_TELEGRAM_CHANNEL_ID", "").strip(),
    "wreath":      _os.getenv("WREATH_TELEGRAM_CHANNEL_ID", "").strip(),
    "conditioner": _os.getenv("CONDITIONER_TELEGRAM_CHANNEL_ID", "").strip(),
    "mcp":         _os.getenv("MCP_TELEGRAM_CHANNEL_ID", "").strip(),
    "kbt":         _os.getenv("KBT_TELEGRAM_CHANNEL_ID", "").strip(),
}

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
    "mcp":         "📦 МБТ",
    "kbt":         "🏠 КБТ",
}
DEFAULT_MODE = "ritual"
MODE_BY_LABEL = {v: k for k, v in MODES_LABELS.items()}

# Какие режимы требуют ввод характеристик пользователем перед генерацией.
# Должен совпадать с requires_specs в локальном config.py агента.
MODES_WITH_SPECS = {"conditioner", "mcp", "kbt"}

# Маркер сообщения-приглашения ввести характеристики. Telegram не отдаёт
# нам напрямую "это reply на ForceReply", но мы можем узнать ответ по
# message.reply_to_message.text — сравнивая с этим уникальным заголовком.
SPECS_PROMPT_HEADER = "📝 Характеристики товара"
BTN_SPECS = "📝 Характеристики"

# Inline-клавиатура выбора режима для ввода характеристик
def _specs_mode_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(MODES_LABELS[m], callback_data=f"specs_mode:{m}")]
        for m in MODES_WITH_SPECS
    ]
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
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
            "ALTER TABLE jobs ADD COLUMN brand TEXT",
            "ALTER TABLE jobs ADD COLUMN model TEXT",
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
        # Heartbeat агента — для /agent_status и алертов
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_heartbeat (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                seen_at TIMESTAMP
            )
        """)
        conn.execute("INSERT OR IGNORE INTO agent_heartbeat (id, seen_at) VALUES (1, NULL)")
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


def get_user_specs(chat_id: int, mode: str | None = None) -> str | None:
    """Характеристики пользователя для конкретного режима (или текущего)."""
    if mode is None:
        mode = get_user_mode(chat_id)
    with db_conn() as conn:
        row = conn.execute(
            "SELECT pending_specs FROM user_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not row or not row["pending_specs"]:
        return None
    try:
        import json
        data = json.loads(row["pending_specs"])
        return data.get(mode) or None
    except Exception:
        # Старый формат — строка без JSON (обратная совместимость)
        return row["pending_specs"] or None


def set_user_specs(chat_id: int, specs: str | None, mode: str | None = None) -> None:
    if mode is None:
        mode = get_user_mode(chat_id)
    import json
    # Читаем текущий JSON
    with db_conn() as conn:
        row = conn.execute(
            "SELECT pending_specs FROM user_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
    try:
        data = json.loads(row["pending_specs"]) if row and row["pending_specs"] else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    if specs:
        data[mode] = specs
    else:
        data.pop(mode, None)
    new_val = json.dumps(data, ensure_ascii=False) if data else None
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO user_state (chat_id, mode, pending_specs, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "pending_specs=excluded.pending_specs, updated_at=excluded.updated_at",
            (chat_id, DEFAULT_MODE, new_val, datetime.now().isoformat()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Извлечение бренда/модели из текста характеристик
# ---------------------------------------------------------------------------
# Юзер может написать в начале списка characteristics:
#   Бренд: Midea
#   Модель: MSAC-12HRN1
#   <дальше преимущества>
# Эти строки выделяются для имени файла, а из специй удаляются (чтобы они
# не попали в плашки преимуществ на карточке).

_BRAND_PREFIXES = ("бренд:", "brand:", "марка:", "производитель:")
_MODEL_PREFIXES = ("модель:", "model:")


def parse_brand_model(specs: str | None) -> tuple[str | None, str | None, str]:
    """Возвращает (brand, model, cleaned_specs).

    Сначала ищет строки 'Бренд: X' / 'Модель: X'.
    Если не найдено — берёт первую строку как brand, вторую как model.
    cleaned_specs всегда содержит полный текст (для подстановки в промпт).
    """
    if not specs:
        return None, None, ""
    brand: str | None = None
    model: str | None = None
    kept_lines: list[str] = []
    for raw in specs.splitlines():
        line = raw.rstrip()
        low = line.lstrip().lower()
        matched = False
        for pref in _BRAND_PREFIXES:
            if low.startswith(pref):
                brand = line.split(":", 1)[1].strip()
                matched = True
                break
        if not matched:
            for pref in _MODEL_PREFIXES:
                if low.startswith(pref):
                    model = line.split(":", 1)[1].strip()
                    matched = True
                    break
        if not matched:
            kept_lines.append(line)
    # Fallback: если не нашли Бренд:/Модель: — первые две строки
    if brand is None and model is None:
        nonempty = [l.strip() for l in specs.splitlines() if l.strip()]
        if nonempty:
            brand = nonempty[0]
        if len(nonempty) >= 2:
            model = nonempty[1]
    cleaned = specs.strip()  # полный текст идёт в промпт
    return (brand or None, model or None, cleaned)


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
BTN_HIDE    = "🔙 Скрыть меню"

# Reply-клавиатура: режимы / характеристики+статус / действия.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(MODES_LABELS["ritual"]),
         KeyboardButton(MODES_LABELS["wreath"])],
        [KeyboardButton(MODES_LABELS["conditioner"]),
         KeyboardButton(MODES_LABELS["mcp"])],
        [KeyboardButton(MODES_LABELS["kbt"])],
        [KeyboardButton(BTN_SPECS), KeyboardButton(BTN_STATUS)],
        [KeyboardButton(BTN_CLEAR), KeyboardButton(BTN_RESTART)],
        [KeyboardButton(BTN_HIDE)],
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

async def cmd_agent_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает, онлайн ли локальный агент (по времени последнего heartbeat)."""
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    with db_conn() as conn:
        row = conn.execute("SELECT seen_at FROM agent_heartbeat WHERE id=1").fetchone()
    if not row or not row["seen_at"]:
        await update.message.reply_text("🔴 Агент: нет данных. Запусти remote_agent.py.", reply_markup=MAIN_KEYBOARD)
        return
    last = datetime.fromisoformat(row["seen_at"])
    age = (datetime.now() - last).total_seconds()
    if age < 30:
        icon = "🟢"
        label = "Онлайн"
    elif age < 120:
        icon = "🟡"
        label = "Нет ответа"
    else:
        icon = "🔴"
        label = "Офлайн"
    pending = _pending_count()
    await update.message.reply_text(
        f"Агент: {icon} {label}\n"
        f"Последний контакт: {int(age)} сек. назад\n"
        f"В очереди: {pending}",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_last(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает последние 5 готовых результатов с кнопкой перегенерации."""
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, mode, output_filename, substr(updated_at,1,16) as ts "
            "FROM jobs WHERE status='done' AND output_filename IS NOT NULL "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
    if not rows:
        await update.message.reply_text("Нет готовых результатов.", reply_markup=MAIN_KEYBOARD)
        return
    lines = []
    buttons = []
    for row in rows:
        label = MODES_LABELS.get(row["mode"], row["mode"])
        lines.append(f"#{row['id']} {label} — {row['output_filename']} ({row['ts']})")
        name_short = row["output_filename"][:28]
        buttons.append([InlineKeyboardButton(
            f"🔄 #{row['id']} {name_short}",
            callback_data=f"redo:{row['id']}",
        )])
    await update.message.reply_text(
        "Последние результаты:\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _allowed(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return
    if update.effective_chat.type != "private":
        from telegram import ReplyKeyboardRemove
        await update.message.reply_text("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())
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
    """Пользователь нажал «📝 Характеристики» — показываем выбор режима."""
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Выбери режим, для которого хочешь задать характеристики:",
        reply_markup=_specs_mode_keyboard(),
    )


# Инструкция по вводу характеристик зависит от режима
_SPECS_INSTRUCTIONS: dict[str, str] = {
    "conditioner": (
        "Первая строка — бренд/производитель (для имени файла).\n"
        "Вторая строка — модель.\n"
        "Далее — преимущества, по одному на строку.\n\n"
        "Пример:\n"
        "Midea\n"
        "MSAC-12HRN1\n"
        "Инверторный компрессор\n"
        "Wi-Fi управление\n"
        "Класс A++"
    ),
    "mcp": (
        "Первая строка — полное название товара (для имени файла).\n"
        "Далее — название, короткая строка и список характеристик.\n\n"
        "Пример:\n"
        "Samsung Galaxy A55 5G 8/256GB\n"
        "Название товара: Samsung Galaxy A55 5G 8/256GB\n"
        "Короткая строка: 8 ядер • 5000mAh • 50Мп\n"
        "Характеристики:\n"
        "- 8-ядерный процессор Exynos 1480\n"
        "- Аккумулятор 5000mAh\n"
        "- Камера 50 Мп"
    ),
    "kbt": (
        "Первая строка — полное название товара (для имени файла).\n"
        "Далее — название, короткая строка и список характеристик.\n\n"
        "Пример:\n"
        "LG LRSOS2706S Side-by-Side\n"
        "Название товара: LG LRSOS2706S Side-by-Side\n"
        "Короткая строка: 635л • No Frost • Wi-Fi\n"
        "Характеристики:\n"
        "- Объём 635 литров\n"
        "- No Frost, автоматическая разморозка\n"
        "- Wi-Fi управление через приложение"
    ),
}


async def _ask_specs_for_mode(query_or_msg, chat_id: int, mode: str) -> None:
    """Показать ForceReply-приглашение для конкретного режима."""
    current = get_user_specs(chat_id, mode)
    label = MODES_LABELS.get(mode, mode)
    current_block = (
        f"\n\nСейчас сохранено ({len(current)} симв.):\n{current[:300]}{'…' if len(current) > 300 else ''}"
        if current else "\n\n(Пока не задано.)"
    )
    instructions = _SPECS_INSTRUCTIONS.get(mode, _SPECS_INSTRUCTIONS["conditioner"])
    text = (
        f"{SPECS_PROMPT_HEADER} — {label}\n\n"
        f"Ответь на это сообщение:\n{instructions}"
        f"{current_block}\n\n"
        "Чтобы сбросить — ответь словом «сброс»."
    )
    if hasattr(query_or_msg, 'message'):
        await query_or_msg.message.reply_text(
            text, reply_markup=ForceReply(selective=True, input_field_placeholder="Бренд: …")
        )
    else:
        await query_or_msg.reply_text(
            text, reply_markup=ForceReply(selective=True, input_field_placeholder="Бренд: …")
        )


async def on_specs_reply(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply на ForceReply-приглашение — сохраняем характеристики для нужного режима."""
    msg = update.message
    if not msg or not msg.text:
        return
    if not update.effective_user or not _allowed(update.effective_user.id):
        return
    text = msg.text.strip()
    if not text:
        return

    # Определяем режим из заголовка родительского сообщения
    parent_text = (msg.reply_to_message.text or "") if msg.reply_to_message else ""
    mode = None
    for m, label in MODES_LABELS.items():
        if label in parent_text:
            mode = m
            break
    if mode is None:
        mode = get_user_mode(msg.chat_id)

    if text.lower() in ("сброс", "reset", "очистить", "clear"):
        set_user_specs(msg.chat_id, None, mode)
        await msg.reply_text(
            f"🗑 Характеристики для «{MODES_LABELS.get(mode, mode)}» сброшены.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    set_user_specs(msg.chat_id, text, mode)
    await msg.reply_text(
        f"✅ Характеристики для «{MODES_LABELS.get(mode, mode)}» сохранены ({len(text)} симв.).\n"
        "Будут автоматически подставлены при следующем фото в этом режиме.\n\n"
        "Чтобы задать другой режим — нажми «📝 Характеристики» снова.\n"
        "Чтобы сбросить — ответь словом «сброс».",
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
    elif text == BTN_HIDE:
        from telegram import ReplyKeyboardRemove
        await update.message.reply_text(
            "Клавиатура скрыта. Напиши /start чтобы вернуть меню.",
            reply_markup=ReplyKeyboardRemove(),
        )


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

    # Лимит очереди — не более 10 pending задач
    user_pending = _pending_count()
    if user_pending >= 10:
        await msg.reply_text(
            f"⚠️ В очереди уже {user_pending} задач. Дождись обработки или нажми «❌ Очистить очередь».",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    filename = f"tg_{ts}{ext}"
    target = INPUT_DIR / filename
    await tg_file.download_to_drive(str(target))
    log.info("Сохранил: %s (%d байт)", filename, target.stat().st_size)

    mode = get_user_mode(msg.chat_id)
    raw_specs = get_user_specs(msg.chat_id, mode) if mode in MODES_WITH_SPECS else None
    # Извлекаем бренд+модель из specs (для имени файла), а из самих специй убираем
    brand, model, specs = parse_brand_model(raw_specs)
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs, brand, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg.chat_id, filename, mode, specs or None, brand, model),
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
    elif raw_specs:
        specs_hint = f"\n📝 С характеристиками ({len(raw_specs)} симв.)"

    # Информация о бренде/модели если распознали
    if brand or model:
        bm = " ".join(filter(None, [brand, model]))
        specs_hint += f"\n🏷 Файл будет сохранён как: {bm}"

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

    # Выбор режима для ввода характеристик
    if data.startswith("specs_mode:"):
        mode = data[len("specs_mode:"):]
        await _ask_specs_for_mode(q, q.message.chat_id, mode)
        return

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

_last_offline_alert: float = 0.0  # когда последний раз слали алерт «агент офлайн»


async def _auto_housekeeping(app: Application) -> None:
    """Фоновые задачи: авто-сброс зависших, очистка файлов, алерты, бэкап БД."""
    global _last_offline_alert
    tick = 0
    while True:
        await asyncio.sleep(60)
        tick += 1
        try:
            # Каждые 5 минут: сбрасываем processing-задачи старше 10 мин
            if tick % 5 == 0:
                cutoff = (datetime.now() - timedelta(minutes=10)).isoformat()
                with db_conn() as conn:
                    n = conn.execute(
                        "UPDATE jobs SET status='pending', updated_at=? "
                        "WHERE status='processing' AND updated_at < ?",
                        (datetime.now().isoformat(), cutoff),
                    ).rowcount
                    if n:
                        conn.commit()
                        log.info("Авто-сброс зависших: %d задач → pending", n)

            # Каждые 5 минут: алерт если очередь стоит >15 мин без агента
            if tick % 5 == 0 and TELEGRAM_ALLOWED_USER_ID:
                with db_conn() as conn:
                    pending_n = conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status='pending'"
                    ).fetchone()[0]
                    hb = conn.execute(
                        "SELECT seen_at FROM agent_heartbeat WHERE id=1"
                    ).fetchone()
                if pending_n > 0 and hb and hb["seen_at"]:
                    age = (datetime.now() - datetime.fromisoformat(hb["seen_at"])).total_seconds()
                    now_ts = _time.time()
                    if age > 900 and (now_ts - _last_offline_alert) > 1800:
                        _last_offline_alert = now_ts
                        try:
                            await app.bot.send_message(
                                chat_id=TELEGRAM_ALLOWED_USER_ID,
                                text=(
                                    f"⚠️ Агент не отвечает {int(age // 60)} мин.\n"
                                    f"В очереди: {pending_n} задач.\n"
                                    "Запусти remote_agent.py на локальном ПК."
                                ),
                            )
                        except Exception as ae:
                            log.warning("Алерт агент-офлайн: %s", ae)

            # Раз в 6 часов: удаляем output-файлы старше 3 дней (уже отправлены)
            if tick % 360 == 0:
                cutoff_date = (datetime.now() - timedelta(days=3)).isoformat()
                with db_conn() as conn:
                    rows = conn.execute(
                        "SELECT output_filename FROM jobs "
                        "WHERE result_sent=1 AND output_filename IS NOT NULL AND updated_at < ?",
                        (cutoff_date,),
                    ).fetchall()
                deleted = 0
                for row in rows:
                    p = OUTPUT_DIR / row["output_filename"]
                    if p.exists():
                        p.unlink(missing_ok=True)
                        deleted += 1
                if deleted:
                    log.info("Авто-очистка output: удалено %d файлов", deleted)

            # Раз в сутки: бэкап queue.db
            if tick % 1440 == 0:
                backup_dir = DB_PATH.parent / "backups"
                backup_dir.mkdir(exist_ok=True)
                backup_name = f"queue_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
                shutil.copy2(DB_PATH, backup_dir / backup_name)
                # Оставляем только последние 7 бэкапов
                old_backups = sorted(backup_dir.glob("queue_*.db"))[:-7]
                for old in old_backups:
                    old.unlink(missing_ok=True)
                log.info("DB бэкап: %s", backup_name)

        except Exception as e:
            log.warning("Housekeeping ошибка: %s", e)


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

                    job_mode = row["mode"] if "mode" in row.keys() else DEFAULT_MODE
                    mode_label = MODES_LABELS.get(job_mode, job_mode)
                    channel_id = MODES_CHANNELS.get(job_mode, "")

                    if channel_id:
                        # Отправляем только в канал режима — без дублирования в личный чат
                        try:
                            with open(out_path, "rb") as f:
                                await app.bot.send_document(
                                    chat_id=int(channel_id),
                                    document=InputFile(f, filename=row["output_filename"]),
                                    caption=row["output_filename"],
                                )
                            log.info("Канал %s: %s", channel_id, row["output_filename"])
                        except Exception as e:
                            log.warning("Ошибка отправки в канал %s: %s", channel_id, e)
                    else:
                        # Канал не настроен — fallback в личный чат
                        keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"redo:{row['id']}")],
                            [InlineKeyboardButton("🗑 Удалить (плохой)", callback_data=f"bad:{row['id']}")],
                        ])
                        pending_now = _pending_count()
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
                        log.info("Личный чат: %s → %s", row["output_filename"], row["chat_id"])

                    with db_conn() as conn:
                        conn.execute("UPDATE jobs SET result_sent=1 WHERE id=?", (row["id"],))
                        conn.commit()
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
        BotCommand("agent_status",  "Онлайн ли локальный агент"),
        BotCommand("last",          "Последние 5 результатов + перегенерация"),
        BotCommand("specs",         "Ввести характеристики товара"),
        BotCommand("clear",         "Снять все ожидающие задачи"),
        BotCommand("restart_stuck", "Пере-запустить зависшие (>5 мин)"),
        BotCommand("help",          "Справка"),
    ])
    # Кнопка «Menu» (рядом с полем ввода) — открывает список команд выше.
    # Без этого вызова Telegram показывает дефолтную кнопку «/», которая для
    # некоторых клиентов выглядит менее заметно.
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    loop.create_task(result_sender(app))
    loop.create_task(_auto_housekeeping(app))

    # Уведомление в Telegram о запуске бота
    if TELEGRAM_ALLOWED_USER_ID:
        try:
            pending = _pending_count()
            await app.bot.send_message(
                chat_id=TELEGRAM_ALLOWED_USER_ID,
                text=f"🟢 ФотоАгент запущен.\nВ очереди: {pending}",
            )
        except Exception as e:
            log.warning("Не удалось отправить уведомление о старте: %s", e)


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
    app.add_handler(CommandHandler("agent_status", cmd_agent_status))
    app.add_handler(CommandHandler("last", cmd_last))
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
    button_labels = list(MODES_LABELS.values()) + [BTN_SPECS, BTN_STATUS, BTN_CLEAR, BTN_RESTART, BTN_HIDE]
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
