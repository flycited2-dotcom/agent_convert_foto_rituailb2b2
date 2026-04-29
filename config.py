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

PROMPTS_DIR = ROOT / "prompts"


@dataclass
class Mode:
    key: str
    label: str               # подпись на кнопке Telegram
    project_url: str         # ChatGPT project URL ("" если не настроен)
    reference_files: list[Path]
    prompt: str              # может содержать плейсхолдер {{SPECS}}
    enabled: bool = True
    # Если True — пользователь должен ввести характеристики перед генерацией.
    # Промпт ОБЯЗАН содержать {{SPECS}}, иначе ввод бессмыслен.
    requires_specs: bool = False
    # Дефолтные характеристики, если пользователь не ввёл свои (fallback).
    default_specs: str = ""

    @property
    def is_configured(self) -> bool:
        """Готов ли режим к работе: есть URL, есть промпт, есть хотя бы 1 эталон."""
        if not self.enabled:
            return False
        if not self.project_url:
            return False
        if not self.prompt or not self.prompt.strip():
            return False
        return any(f.exists() for f in self.reference_files) and bool(self.reference_files)

    def render_prompt(self, specs: str | None = None) -> str:
        """Подставить характеристики в плейсхолдер {{SPECS}}."""
        if "{{SPECS}}" not in self.prompt:
            return self.prompt
        value = (specs or "").strip() or self.default_specs
        return self.prompt.replace("{{SPECS}}", value)


def _mode_refs(key: str) -> list[Path]:
    """Все эталоны режима: reference/<key>/etalon_*.png в алфавитном порядке."""
    folder = REFERENCE_DIR / key
    if not folder.exists():
        return []
    return sorted(folder.glob("etalon_*.png"))


def _mode_prompt(key: str, env_var: str, fallback: str = "") -> str:
    """
    Промпт читается в порядке приоритета:
      1) prompts/<key>.txt — основной способ (длинные многострочные тексты)
      2) переменная окружения <env_var> — для оперативной правки без файла
      3) fallback (пустая строка → режим помечается как НЕ настроен)
    """
    prompt_file = PROMPTS_DIR / f"{key}.txt"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8").strip()
    return os.getenv(env_var, fallback).strip()


MODES: dict[str, Mode] = {
    "ritual": Mode(
        key="ritual",
        label="🧺 Корзинки",
        project_url=os.getenv(
            "RITUAL_PROJECT_URL",
            os.getenv("CHATGPT_PROJECT_URL", ""),  # совместимость
        ).strip(),
        reference_files=_mode_refs("ritual"),
        prompt=_mode_prompt("ritual", "RITUAL_PROMPT"),
        enabled=True,
    ),
    "wreath": Mode(
        key="wreath",
        label="⚜️ Венки",
        project_url=os.getenv("WREATH_PROJECT_URL", "").strip(),
        reference_files=_mode_refs("wreath"),
        prompt=_mode_prompt("wreath", "WREATH_PROMPT"),
        enabled=True,
    ),
    "conditioner": Mode(
        key="conditioner",
        label="❄️ Кондиционеры",
        project_url=os.getenv("CONDITIONER_PROJECT_URL", "").strip(),
        reference_files=_mode_refs("conditioner"),
        prompt=_mode_prompt("conditioner", "CONDITIONER_PROMPT"),
        enabled=True,
        requires_specs=True,
        default_specs=(
            "Автоматическое качание заслонок\n"
            "Режим «Комфортный сон»\n"
            "Автоматическая очистка теплообменника\n"
            "Тёплый пуск\n"
            "Многоступенчатая очистка воздуха\n"
            "Фильтр высокой степени очистки\n"
            "Антикоррозийное покрытие теплообменника\n"
            "Защита от коррозии\n"
            "Wi-Fi Control — опция"
        ),
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
