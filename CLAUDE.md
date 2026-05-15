# Memory / Handoff для RitualB2B Multi-Mode Photo Bot

Этот файл загружается Claude Code автоматически в каждой сессии для этого
проекта. Здесь — компактное состояние системы и важные факты, без которых
следующая сессия будет всё переспрашивать.

## Что это вообще

Telegram-бот для пользователя @flycited (Алексей Царёв, владелец магазина
ритуальных товаров + смежные товары). Бот принимает фото товара, и через
автоматизацию Chrome→ChatGPT-проектов превращает их в премиальные карточки
для сайта и Telegram-каналов. Один бот — много типов товаров через режимы.

## Архитектура (важно!)

```
[Пользователь в Telegram]
        │ фото + кнопки режимов
        ▼
[VPS 213.109.202.45]                          [Локальный ПК пользователя]
  ritualb2b-bot.service     ← SSH-туннель ← remote_agent.py (start_remote_agent.bat)
  ritualb2b-api.service        порт 8765        │
  SQLite queue.db                                ▼
  /root/ritualb2b/                          Chrome (через CDP :9333)
                                                 │
                                                 ▼
                                          ChatGPT-проекты
                                          (по одному на режим)
```

- **VPS** хранит бот + очередь (SQLite). Никогда не "ходит" в ChatGPT.
- **Локальный ПК** запускает Chrome через `start_chrome.bat`, потом
  `remote_agent.py` поллит API VPS через SSH-туннель и обрабатывает задачи
  через playwright/CDP.
- **Google Drive** загружается из `remote_agent.py` (локально), у каждого
  режима своя папка (см. .env, переменные `*_GDRIVE_FOLDER_ID`).

## Продакшен VPS (актуальный)

- **IP:** `213.109.202.45`
- **Пользователь:** `root`
- **Пароль:** в локальном `.env` (`VPS_SSH_PASS`, без `@` на конце)
- **Путь проекта:** `/root/ritualb2b/`
- **Сервисы:** `ritualb2b-bot.service`, `ritualb2b-api.service` (systemd, autostart)
- **API порт:** `8765` (только локально внутри VPS; снаружи — через SSH-туннель)

Старый VPS `186.246.44.204` мигрирован 2026-05-15, сервисы там остановлены.
После проверки нового — можно сносить (`/root/ritualb2b/` + systemd unit-файлы
+ архив `/tmp/ritualb2b_migration.tar.gz`).

**Важный нюанс SFTP:** на новом VPS была сломана конфигурация
`Subsystem sftp internal-sftp-server` (нет такого), починили на
`Subsystem sftp /usr/lib/openssh/sftp-server`. Если потом снова сломается —
проверять `/etc/ssh/sshd_config`.

## Режимы (типы товаров)

| key | label | requires_specs | Статус |
|---|---|---|---|
| `ritual` | 🧺 Корзинки | нет | работает |
| `wreath` | ⚜️ Венки | нет | работает |
| `conditioner` | ❄️ Кондиционеры SplitHub | ДА | работает |
| `mcp` | МБТ (мелкая бытовая) | ДА | работает |
| `kbt` | КБТ (крупная бытовая) | ДА | работает |

**Конфигурация режима** хранится в трёх местах:
- `config.py::MODES` — словарь `Mode(...)` с URL проекта, эталонами, промптом
- `reference/<key>/etalon_*.png` — эталоны стиля (glob, может быть 1+)
- `prompts/<key>.txt` — основной промпт (с плейсхолдером `{{SPECS}}` если режим
  с specs)

**Specs (характеристики)** для режимов из `MODES_WITH_SPECS`:
- Пользователь нажимает «📝 Характеристики» в боте, выбирает режим, отвечает
  на ForceReply
- Бот парсит через `parse_brand_model(specs)` — 4-ступенчатая стратегия
  (явные префиксы → inline-ключевые слова → тип устройства → first-word fallback)
- Бренд и модель идут в имя файла (`Midea_MSAC-12HRN1_2026-05-15_001.png`)
- Остальной текст — в `{{SPECS}}` промпта (плашки преимуществ)

## Важные файлы

```
agent.py              — обработка одного фото через Chrome (process_one_file)
remote_agent.py       — поллер задач с VPS, SSH-туннель, цикл retry
config.py             — MODES + Mode dataclass + get_mode/slugify
prompts/<key>.txt     — промпты для каждого режима
reference/<key>/      — эталоны
vps/vps_bot.py        — Telegram-бот (живёт на VPS)
vps/vps_api.py        — FastAPI для агента (живёт на VPS)
vps/config_vps.py     — конфиг бота
.env                  — секреты (в .gitignore!), .env.example в репо
start_chrome.bat      — запустить Chrome с remote-debugging-port
start_remote_agent.bat — запустить remote_agent.py
stop_local_bots.bat   — убить локальные python-процессы бота/агента
```

## Типичные операции

### Деплой изменений на VPS
```python
# Через paramiko из локального Python:
import paramiko
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('213.109.202.45', username='root', password='<.env VPS_SSH_PASS>', timeout=30)
sftp = c.open_sftp()
sftp.put('vps/vps_bot.py', '/root/ritualb2b/vps_bot.py')
sftp.put('vps/vps_api.py', '/root/ritualb2b/vps_api.py')
c.exec_command('systemctl restart ritualb2b-bot ritualb2b-api')
```

### Проверка состояния
- `systemctl is-active ritualb2b-bot ritualb2b-api` на VPS
- `journalctl -u ritualb2b-bot --since="2 minutes ago" --no-pager`
- Локально: `logs/remote_agent.log`, `logs/agent.log`

### Добавление нового режима
1. Добавить ключ в `MODES_LABELS` в `vps/vps_bot.py`
2. Добавить `Mode(...)` в `config.py` для агента
3. Создать `reference/<key>/etalon_*.png`
4. Создать `prompts/<key>.txt`
5. В `.env` — переменные `<KEY>_PROJECT_URL`, опционально `<KEY>_GDRIVE_FOLDER_ID`
6. Если режим требует specs — добавить в `MODES_WITH_SPECS` (бот) и поставить
   `requires_specs=True` + `default_specs` (агент)
7. Залить vps_bot.py на VPS, перезапустить bot/api

## Что НЕ нужно делать

- Никогда не запускать локальный `bot.py` параллельно с VPS-ботом — оба
  используют один и тот же Telegram токен, будет `Conflict` и боты будут
  "красть" сообщения друг у друга
- Не коммитить `.env`, `gdrive_token.json`, `client_secret_*.json` (все в
  `.gitignore`)
- Не коммитить `output/*`, `input/*`, `processed/*`, `failed/*`, `logs/*`
  (тоже в `.gitignore`)
- Не использовать `--no-verify` или `--amend` на git без явной просьбы
- Не запускать `remote_agent.py` если Chrome не открыт через
  `start_chrome.bat` (нужен `--remote-debugging-port=9333`)

## Текущая глобальная политика

В пользовательском глобальном `~/.claude/CLAUDE.md` стоит правило: всегда
применять `karpathy-guidelines` skill (думать перед кодом, simplicity first,
surgical changes, goal-driven execution). Это применяется автоматически.
