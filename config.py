import os
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

# URL проекта ChatGPT. Если задан — агент работает в режиме проекта:
# загружает только фото товара (эталоны уже в проекте), быстрее и надёжнее.
# Оставь пустым для работы в обычном режиме (3 файла + полный промпт).
CHATGPT_PROJECT_URL = os.getenv("CHATGPT_PROJECT_URL", "").strip()

# Короткое сообщение в режиме проекта (инструкции уже в настройках проекта)
PROJECT_PROMPT = os.getenv("PROJECT_PROMPT", "Вот фото товара. Сгенерируй карточку.")

INPUT_DIR = _path("INPUT_DIR", "input")
OUTPUT_DIR = _path("OUTPUT_DIR", "output")
PROCESSED_DIR = _path("PROCESSED_DIR", "processed")
FAILED_DIR = _path("FAILED_DIR", "failed")
REFERENCE_DIR = _path("REFERENCE_DIR", "reference")
LOGS_DIR = _path("LOGS_DIR", "logs")

GENERATION_TIMEOUT_SEC = int(os.getenv("GENERATION_TIMEOUT_SEC", "300"))
DELAY_BETWEEN_JOBS_SEC = int(os.getenv("DELAY_BETWEEN_JOBS_SEC", "5"))

REFERENCE_FILES = [
    REFERENCE_DIR / "etalon_1.png",
    REFERENCE_DIR / "etalon_2.png",
]
TZ_FILE = REFERENCE_DIR / "tz.txt"

for d in (INPUT_DIR, OUTPUT_DIR, PROCESSED_DIR, FAILED_DIR, REFERENCE_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)


PROMPT_TEMPLATE = """Я прикрепил 3 фото:

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
