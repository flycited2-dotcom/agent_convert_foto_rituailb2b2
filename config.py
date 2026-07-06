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
    # ID папки Google Drive специально для этого режима. Пусто — используется
    # общая GDRIVE_FOLDER_ID для всех режимов.
    gdrive_folder_id: str = ""
    # ID Telegram-канала для пересылки готовых карточек. Пусто — не пересылаем.
    telegram_channel_id: str = ""
    # False — режиму не нужны эталоны стиля (research: текст+веб-поиск, не карточка).
    requires_reference: bool = True

    @property
    def is_configured(self) -> bool:
        """Готов ли режим к работе: есть URL, есть промпт, есть хотя бы 1 эталон
        (для режимов с requires_reference)."""
        if not self.enabled:
            return False
        if not self.project_url:
            return False
        if not self.prompt or not self.prompt.strip():
            return False
        if self.requires_reference:
            return any(f.exists() for f in self.reference_files) and bool(self.reference_files)
        return True

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
        gdrive_folder_id=os.getenv("RITUAL_GDRIVE_FOLDER_ID", "").strip(),
        telegram_channel_id=os.getenv("RITUAL_TELEGRAM_CHANNEL_ID", "").strip(),
    ),
    "wreath": Mode(
        key="wreath",
        label="⚜️ Венки",
        project_url=os.getenv("WREATH_PROJECT_URL", "").strip(),
        reference_files=_mode_refs("wreath"),
        prompt=_mode_prompt("wreath", "WREATH_PROMPT"),
        enabled=True,
        gdrive_folder_id=os.getenv("WREATH_GDRIVE_FOLDER_ID", "").strip(),
        telegram_channel_id=os.getenv("WREATH_TELEGRAM_CHANNEL_ID", "").strip(),
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
            "Инверторный компрессор\n"
            "Класс энергоэффективности A++\n"
            "Режим «Комфортный сон»\n"
            "Тёплый пуск\n"
            "Самоочистка теплообменника\n"
            "Антикоррозионное покрытие\n"
            "Wi-Fi управление (опция)"
        ),
        gdrive_folder_id=os.getenv("CONDITIONER_GDRIVE_FOLDER_ID", "").strip(),
        telegram_channel_id=os.getenv("CONDITIONER_TELEGRAM_CHANNEL_ID", "").strip(),
    ),
    "mcp": Mode(
        key="mcp",
        label="📦 МБТ",
        project_url=os.getenv("MCP_PROJECT_URL", "").strip(),
        reference_files=_mode_refs("mcp"),
        prompt=_mode_prompt("mcp", "MCP_PROMPT"),
        enabled=True,
        requires_specs=True,
        default_specs="",
        gdrive_folder_id=os.getenv("MCP_GDRIVE_FOLDER_ID", "").strip(),
        telegram_channel_id=os.getenv("MCP_TELEGRAM_CHANNEL_ID", "").strip(),
    ),
    "kbt": Mode(
        key="kbt",
        label="🏠 КБТ",
        project_url=os.getenv("KBT_PROJECT_URL", "").strip(),
        reference_files=_mode_refs("kbt"),
        prompt=_mode_prompt("kbt", "KBT_PROMPT"),
        enabled=True,
        requires_specs=True,
        default_specs="",
        gdrive_folder_id=os.getenv("KBT_GDRIVE_FOLDER_ID", "").strip(),
        telegram_channel_id=os.getenv("KBT_TELEGRAM_CHANNEL_ID", "").strip(),
    ),
    # research — не карточка: по наименованию найти в интернете УТП и изображение
    # товара (для контент-завода). Эталоны не нужны, {{SPECS}} = «Категория Бренд Модель».
    "research": Mode(
        key="research",
        label="🔎 Research (фото+УТП по названию)",
        project_url=os.getenv("RESEARCH_PROJECT_URL", "").strip(),
        reference_files=[],
        prompt=_mode_prompt("research", "RESEARCH_PROMPT"),
        enabled=True,
        requires_specs=True,
        requires_reference=False,
    ),
}

DEFAULT_MODE = "ritual"

# Второй шаг research-задачи: запрос изображения ОТДЕЛЬНЫМ сообщением (после УТП).
# Причина: в одном запросе модель часто сразу генерит картинку и не пишет список УТП.
RESEARCH_IMAGE_PROMPT = _mode_prompt("research_image", "RESEARCH_IMAGE_PROMPT")


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


# ---------------------------------------------------------------------------
# Дорожки (lanes) — мульти-аккаунт ChatGPT (Phase 3 спеки 2026-07-06).
# lanes.json: карта машин (MachineGuid → имя) + дорожки с ПРИВЯЗКОЙ к машине.
# my_lanes() отдаёт только дорожки своей машины — иначе оба вотчдога подняли бы
# все дорожки и задвоили аккаунты (ревью 2026-07-06). project_url на дорожку —
# литералом или "env:ИМЯ" (URL остаётся в .env, lanes.json — в git).
# ---------------------------------------------------------------------------
LANES_FILE = ROOT / "lanes.json"


@dataclass
class Lane:
    id: str
    machine: str
    cdp_port: int
    profile_dir: str = "chrome_profile"
    enabled: bool = True
    project_urls: dict | None = None

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.cdp_port}"

    def project_url_for(self, mode_key: str) -> str:
        """Переопределение project_url дорожки для режима: литеральный URL или
        'env:ИМЯ' (значение из .env). Пусто → '' — вызывающий падает обратно
        на Mode.project_url (дорожка акк1 живёт на текущих env-переменных)."""
        raw = ((self.project_urls or {}).get(mode_key) or "").strip()
        if raw.startswith("env:"):
            return os.getenv(raw[4:], "").strip()
        return raw


def machine_id() -> str:
    """Идентичность машины: env LANE_MACHINE_GUID (тесты/не-Windows) или
    Windows MachineGuid (стабилен, не меняется при переименовании ПК)."""
    env = os.getenv("LANE_MACHINE_GUID", "").strip()
    if env:
        return env
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0])
    except (OSError, ImportError):
        return ""


def _lanes_raw(path: Path | None = None) -> dict:
    import json
    p = path or LANES_FILE
    if not Path(p).exists():
        return {}
    return json.loads(Path(p).read_text(encoding="utf-8"))


def load_lanes(path: Path | None = None) -> list[Lane]:
    """Все дорожки из lanes.json (нет файла → пусто = поведение как раньше)."""
    return [Lane(id=l["id"], machine=l.get("machine", ""),
                 cdp_port=int(l["cdp_port"]),
                 profile_dir=l.get("profile_dir", "chrome_profile"),
                 enabled=bool(l.get("enabled", True)),
                 project_urls=l.get("project_urls") or {})
            for l in _lanes_raw(path).get("lanes", [])]


def my_lanes(path: Path | None = None) -> list[Lane]:
    """Включённые дорожки ЭТОЙ машины (по MachineGuid через карту machines).
    GUID не в карте → пусто: безопасный дефолт — чужие дорожки не поднимать."""
    name = _lanes_raw(path).get("machines", {}).get(machine_id(), "")
    return [l for l in load_lanes(path) if l.enabled and l.machine == name]


def get_lane(lane_id: str, path: Path | None = None) -> Lane | None:
    for lane in load_lanes(path):
        if lane.id == lane_id:
            return lane
    return None
