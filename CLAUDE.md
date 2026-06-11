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
- **Доступ: ТОЛЬКО по SSH-ключу** (вход по паролю отключён 2026-06-11,
  `sshd_config.d/00-ritualb2b-hardening.conf`: `PasswordAuthentication no`,
  `PermitRootLogin prohibit-password`). Ключи на локальном ПК в `~/.ssh/`:
  - `id_ritualb2b_admin` — полный доступ для деплоя (paramiko `key_filename=`)
  - `id_ritualb2b_agent` — ОГРАНИЧЕН пробросом порта 8765 (`restrict,
    permitopen,command="/bin/false"` в authorized_keys), shell не даёт.
    Путь в `.env` → `VPS_SSH_KEY`. `VPS_SSH_PASS` пустой.
- **Путь проекта:** `/root/ritualb2b/`
- **Сервисы:** `ritualb2b-bot.service`, `ritualb2b-api.service` (systemd, autostart)
- **API порт:** `8765` (только локально внутри VPS; снаружи закрыт firewall
  `INPUT DROP`; для агента — через SSH-туннель)

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
ssh_tunnel.py         — класс SSHTunnel (общий для агента и вотчдога)
agent_watchdog.py     — вотчдог: поллит VPS, по кнопке из Telegram поднимает
                        Chrome+агента. Автозапуск: задача планировщика
                        RitualB2B_Watchdog (onlogon). Лог: logs/watchdog.log
start_watchdog.bat    — ручной запуск вотчдога
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
# Деплой — АДМИНСКИМ ключом (пароль на VPS отключён). look_for_keys/allow_agent=False
# чтобы paramiko не перебирал чужие ключи и не упёрся в MaxAuthTries.
c.connect('213.109.202.45', username='root',
          key_filename=r'C:\Users\TLT-1\.ssh\id_ritualb2b_admin',
          look_for_keys=False, allow_agent=False, timeout=30)
# SFTP на VPS нестабилен ("EOF during negotiation") — заливать base64-чанками
# по SSH (см. memory deploy-via-paramiko-not-sftp), затем py_compile + restart.
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

## Критические факты (грабли, на которые уже наступали)

### ⚠️ Вставка промпта — ТОЛЬКО `insert_text`, НЕ clipboard
`agent.py::paste_text` ОБЯЗАН использовать `page.keyboard.insert_text(text)`,
а НЕ `copy_text_to_clipboard` + `Ctrl+V`. Причина: ChatGPT превращает длинную
вставку из буфера обмена в **файл-вложение «Вставленный текст.txt»**, который
модель НЕ читает как инструкцию — она игнорирует `{{SPECS}}` и копирует
характеристики/текст с эталонных картинок. Симптом: «характеристики не
меняются», на карточке появляются плашки с эталона вместо введённых
пользователем. Фикс — коммит `b6f1839` (2026-05-20).
(В Playwright Python метод — `insert_text`, snake_case. Не `insertText`.)

### Эталоны не должны «протекать» в контент
Эталоны кондиционера (`reference/conditioner/etalon_*.png`) — это карточки
KENTATSU с готовыми плашками характеристик. Модель склонна копировать ИХ текст.
В `prompts/conditioner.txt` добавлены явные запреты копировать характеристики
с Фото 1/Фото 2 — брать только из раздела «Список преимуществ» ({{SPECS}}).

### Google Drive (загрузка результатов)
- Работает из `remote_agent.py` (локально), у каждого режима своя папка.
- Нужны 2 файла рядом с `gdrive.py` (оба в `.gitignore`):
  `gdrive_oauth_client.json` (OAuth desktop client) + `gdrive_token.json` (токен).
- Проект Google Cloud: `foto-ritualb2b-korzinki`, аккаунт `flycited2@gmail.com`.
- Включатель в `.env`: `GDRIVE_CREDENTIALS_JSON=gdrive_oauth_client.json`
  (если пусто — загрузка отключена). Папки: `*_GDRIVE_FOLDER_ID` для 5 режимов.
- Повторная авторизация: открыть auth URL в Chrome через расширение (аккаунт
  уже залогинен), callback ловит локальный сервер на порту 8788.

### Перезапуск remote_agent.py после правок agent.py/config.py/prompts
`config.py` читает промпты при импорте — изменения промптов/кода подхватываются
ТОЛЬКО после рестарта `remote_agent.py`. Запуск (Chrome уже открыт на :9333):
`nohup python remote_agent.py > logs/agent_nohup.out 2>&1 &` (PowerShell
Start-Process и `cmd start` в этой среде работали ненадёжно).

### Безопасность доступа к VPS (hardening 2026-06-11)
Вход по паролю отключён, только SSH-ключи. Два ключа в `~/.ssh/` на ПК
(см. раздел «Продакшен VPS»). authorized_keys на VPS:
```
<admin_pub>                                              # полный доступ
restrict,port-forwarding,permitopen="127.0.0.1:8765",command="/bin/false" <agent_pub>
```
ВАЖНО: `restrict` НЕ блокирует exec-канал сам по себе — нужен
`command="/bin/false"`, иначе агентский ключ даёт shell. direct-tcpip
(проброс порта) при этом продолжает работать — forced command применяется
только к session/exec-каналам. Конфиг в `sshd_config.d/00-*.conf` (читается
ПЕРВЫМ, перебивает cloud-init `50-*` с `PasswordAuthentication yes`).
Перед `systemctl restart ssh` всегда `sshd -t`. Откат при потере ключа —
через Hestia panel (порт 8083) или консоль провайдера.
Остаточный риск: пароль root всё ещё валиден для Hestia/консоли (по SSH
не пускает) — при желании сменить отдельно.

### Управление агентом кнопками из Telegram (добавлено 2026-06-11)
Кнопки бота «🚀 Запустить агента» / «🔁 Перезапуск агента» / «⛔ Стоп агента»
→ INSERT в таблицу `flags` (`agent_command='start'|'restart'|'stop'`) в
queue.db → вотчдог на ПК (agent_watchdog.py, поллит `GET /api/agent-command`
каждые 15 сек) исполняет: start — поднять Chrome (если CDP мёртв) +
remote_agent.py (если не запущен); restart — убить агента и поднять заново;
stop — убить агента. Бот подтверждает результат по heartbeat (start/restart —
через 2 мин, stop — через 1 мин). Эндпоинт отдаёт команду ровно один раз
(сразу удаляет флаг). Убийство агента — PowerShell Stop-Process по
CommandLine match 'remote_agent'. Все три команды протестированы e2e.

### Долгоживущие процессы НЕ запускать из Claude-инструментов
Процессы, запущенные через Start-Process/Popen из тулов Claude Code,
умирают вместе с завершением тул-колла (харнес чистит дерево процессов).
Запускать только через Планировщик: `schtasks /run /tn RitualB2B_Watchdog`
(вотчдог сам поднимет агента через флаг) — или руками через .bat.
PowerShell-тул (pwsh 7) в песочнице не видит чужие процессы; использовать
Bash → `powershell -Command` (но Git Bash мангрит `$_` и `/root/...` —
для VPS-путей ставить `MSYS_NO_PATHCONV=1`).

### SSH-туннель рвётся во время долгой генерации (фикс 2026-06-11)
Paramiko-туннель умирал за 3–7 мин генерации ChatGPT; агент вечно долбил
мёртвый локальный порт («Сеть: — жду 30 сек.» в логе), а готовый результат
удалялся в `finally`. Фикс в `remote_agent.py`: счётчик `net_errors` (2 подряд
→ raise → main() пересоздаёт туннель), буфер `PENDING_UPLOADS` (недоставленный
результат досылается после реконнекта), keepalive 10 сек. Симптом в Telegram:
бот пишет «Агент не отвечает N мин» при живом процессе агента.

### Застрявшие задачи
Если убить агента в момент генерации — задача остаётся в статусе `processing`
и не переобрабатывается. Сбросить: `UPDATE jobs SET status='pending' WHERE id=N`
в `/root/ritualb2b/queue.db` на VPS (через paramiko + heredoc-скрипт; SFTP на
VPS работает нестабильно — заливать файлы через base64-чанки по SSH).

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
