import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    if not p.is_absolute():
        p = ROOT / p
    return p


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0") or 0)

CHROME_CDP_URL = os.getenv("CHROME_CDP_URL", "http://127.0.0.1:9333").strip()

INPUT_DIR = _path("INPUT_DIR", "input")
OUTPUT_DIR = _path("OUTPUT_DIR", "output")
PROCESSED_DIR = _path("PROCESSED_DIR", "processed")
FAILED_DIR = _path("FAILED_DIR", "failed")
REFERENCE_DIR = _path("REFERENCE_DIR", "reference")
LOGS_DIR = _path("LOGS_DIR", "logs")

GDRIVE_CREDENTIALS_JSON = os.getenv("GDRIVE_CREDENTIALS_JSON", "").strip()
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()

GENERATION_TIMEOUT_SEC = int(os.getenv("GENERATION_TIMEOUT_SEC", "300"))
DELAY_BETWEEN_JOBS_SEC = int(os.getenv("DELAY_BETWEEN_JOBS_SEC", "5"))

for d in (INPUT_DIR, OUTPUT_DIR, PROCESSED_DIR, FAILED_DIR, REFERENCE_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# РЕЖИМЫ (типы товаров)
# ---------------------------------------------------------------------------
# Каждый режим = свой ChatGPT-проект + свои эталоны + свой промпт.
# Чтобы добавить новый товар: создай ChatGPT-проект, положи эталоны в
# reference/<key>/etalon_*.png, добавь Mode(...) ниже, выстави enabled=True.
#
# Эталоны лежат в reference/<key>/ — etalon_1.png, etalon_2.png (можно больше).
# project_url — URL проекта в ChatGPT (берётся из .env).

@dataclass
class Mode:
    key: str
    label: str               # подпись на кнопке Telegram
    project_url: str         # ChatGPT project URL ("" если не настроен)
    reference_files: list[Path]
    prompt: str
    enabled: bool = True

    @property
    def is_configured(self) -> bool:
        """Готов ли режим к работе: есть URL и все эталоны существуют."""
        if not self.enabled:
            return False
        if not self.project_url:
            return False
        return all(f.exists() for f in self.reference_files)


def _mode_refs(key: str) -> list[Path]:
    """Эталоны режима: reference/<key>/etalon_1.png, etalon_2.png."""
    return [
        REFERENCE_DIR / key / "etalon_1.png",
        REFERENCE_DIR / key / "etalon_2.png",
    ]


_RITUAL_PROMPT = """Я прикрепил 3 фото:

- Фото 1 и Фото 2 — ЭТАЛОНЫ СТИЛЯ карточки товара (как должна выглядеть готовая карточка для сайта). Светло-серый студийный фон с премиальным градиентом, заголовок "РИТУАЛЬНАЯ КОМПОЗИЦИЯ" зелёным сверху, подзаголовок "ПРЕМИАЛЬНОЕ ИСПОЛНЕНИЕ", три бейджа снизу.
- Фото 3 — ИСХОДНЫЙ ТОВАР (снят в произвольной обстановке), который нужно поместить на новую карточку.

Задача: сгенерируй ПРЕМИАЛЬНУЮ КАРТОЧКУ ТОВАРА на основе ИСХОДНОГО фото (фото 3), оформив её в едином стиле с эталонами (фото 1 и фото 2). Эталоны — это ТОЛЬКО референс стиля, фона, типографики и компоновки. НЕ переноси с них сам товар.

ВАЖНО:
1. Товар с фото 3 сохрани АБСОЛЮТНО ИДЕНТИЧНО: те же цветы, тот же декор, ленты, бабочки, мишура — все элементы, которые есть на исходном фото. Не заменяй товар на тот, что на эталонах. Не добавляй и не убирай ни один элемент товара.
2. Убери ВСЁ лишнее с фона (мебель, стены, плинтус, любую домашнюю обстановку).
3. Размести товар по центру кадра.
4. Сделай чистый студийный светло-графитовый фон с мягким премиальным градиентом — как на эталонах.
5. Сверху размести заголовок "РИТУАЛЬНАЯ КОМПОЗИЦИЯ" (зелёным цветом), декоративный разделитель и подзаголовок "ПРЕМИАЛЬНОЕ ИСПОЛНЕНИЕ".
6. Снизу — три бейджа с иконками и подписями: "АККУРАТНЫЙ ДЕКОР", "ВЫРАЗИТЕЛЬНАЯ КОМПОЗИЦИЯ", "ЭЛЕГАНТНАЯ ПОДАЧА".
7. Формат 1:1 (квадрат), стиль современный, дорогой, минималистичный, каталожный. Без шумов, без посторонних теней, без визуального мусора.
8. Финал должен выглядеть как качественная единая карточка товара для сайта, в одной серии с эталонами.

Сгенерируй итоговое изображение."""


_WREATH_PROMPT = os.getenv(
    "WREATH_PROMPT",
    # TODO: заполните когда создадите ChatGPT-проект для венков.
    # Шаблон ниже — ритуальный, отредактируйте под венки (заголовки, бейджи).
    _RITUAL_PROMPT,
)

_CONDITIONER_PROMPT = os.getenv(
    "CONDITIONER_PROMPT",
    # TODO: заполните когда создадите ChatGPT-проект для кондиционеров.
    "Сгенерируй премиальную карточку товара для кондиционера на основе исходного фото. "
    "Чистый студийный фон, товар по центру, формат 1:1, в стиле эталонов из проекта.",
)


MODES: dict[str, Mode] = {
    "ritual": Mode(
        key="ritual",
        label="🕯 Ритуал",
        project_url=os.getenv(
            "RITUAL_PROJECT_URL",
            # совместимость со старой переменной CHATGPT_PROJECT_URL
            os.getenv("CHATGPT_PROJECT_URL", ""),
        ).strip(),
        reference_files=_mode_refs("ritual"),
        prompt=_RITUAL_PROMPT,
        enabled=True,
    ),
    "wreath": Mode(
        key="wreath",
        label="⚜️ Венки",
        project_url=os.getenv("WREATH_PROJECT_URL", "").strip(),
        reference_files=_mode_refs("wreath"),
        prompt=_WREATH_PROMPT,
        enabled=True,  # сам режим включён, но is_configured=False пока нет URL+эталонов
    ),
    "conditioner": Mode(
        key="conditioner",
        label="❄️ Кондиционеры",
        project_url=os.getenv("CONDITIONER_PROJECT_URL", "").strip(),
        reference_files=_mode_refs("conditioner"),
        prompt=_CONDITIONER_PROMPT,
        enabled=True,
    ),
}

DEFAULT_MODE = "ritual"


def get_mode(key: str | None) -> Mode:
    """Безопасно достать режим по key. Если ключа нет — возвращает default."""
    if key and key in MODES:
        return MODES[key]
    return MODES[DEFAULT_MODE]


# ---------------------------------------------------------------------------
# Совместимость со старым кодом (агент использовал CHATGPT_PROJECT_URL и пр.).
# Сейчас агент работает через MODES, эти переменные оставлены как fallback
# для скриптов, которые могут их использовать (CLI, тесты).
# ---------------------------------------------------------------------------
CHATGPT_PROJECT_URL = MODES[DEFAULT_MODE].project_url
PROMPT_TEMPLATE     = MODES[DEFAULT_MODE].prompt
REFERENCE_FILES     = MODES[DEFAULT_MODE].reference_files
TZ_FILE             = REFERENCE_DIR / "tz.txt"
