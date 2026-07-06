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
agent_watchdog.py     — вотчдог: поллит VPS, по кнопке из Telegram поднимает/
                        перезапускает/останавливает агента + Chrome. Фоновый
                        (pythonw, без окна). Автозапуск+самовосстановление:
                        задача планировщика RitualB2B_Watchdog (time-триггер
                        каждые 5 мин + logon, MultipleInstances=Parallel +
                        guard в коде от дублей). Лог: logs/watchdog.log
start_watchdog.bat    — ручной запуск вотчдога (с окном; для отладки)
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

### Управление агентом кнопками из Telegram (2026-06-11, надёжность 2026-06-12)
Кнопки бота «🚀 Запустить агента» / «🔁 Перезапуск агента» / «⛔ Стоп агента»
→ INSERT в таблицу `flags` (`agent_command='start'|'restart'|'stop'`) в
queue.db → вотчдог на ПК (agent_watchdog.py, поллит `GET /api/agent-command`
каждые 15 сек) исполняет:
- **start** — поднять Chrome (если CDP мёртв) + remote_agent.py (если не
  запущен). Chrome проверяется ПЕРВЫМ, независимо от агента (грабля
  2026-06-12: было наоборот — START при живом агенте не поднимал Chrome).
- **restart** — убить агента И ботовский Chrome (только его: фильтр chrome.exe
  по `remote-debugging-port=9333` в CommandLine, личный Chrome не трогаем),
  поднять оба заново. Лекарство от любых зависаний.
- **stop** — убить агента и не воскрешать.
Желаемое состояние хранится в `logs/agent_state.txt` (running/stopped;
start/restart → running, stop → stopped). Раз в ~минуту вотчдог сверяет
реальность с желаемым: running + умерший Chrome/агент → поднимает
(самовосстановление после перезагрузки ПК, краша и т.п.).
Бот подтверждает результат по heartbeat (start/restart — через 2 мин,
stop — через 1 мин). Эндпоинт отдаёт команду ровно один раз (сразу удаляет
флаг). Убийство агента — PowerShell Stop-Process по CommandLine match
'remote_agent'.

### Защита очереди от сжигания и дублей (2026-06-12)
- Агент перед взятием задачи пингует Chrome CDP; мёртв — задачи НЕ берёт
  (раньше: брал → ECONNREFUSED ×3 быстрые попытки → failed за минуту).
- Задача без input_filename (битая) помечается failed, а не роняет агента
  в вечный цикл Path(None).
- Авто-сброс зависших processing-задач в боте: порог 30 мин (НЕ снижать:
  легитимная обработка с 3 ретраями — до ~15 мин, ранний сброс = повторная
  обработка и дубль результата).

⚠️ ГРАБЛИ: фильтр процессов по CommandLine match ловит САМ powershell-процесс
(его команда содержит искомую строку) → ложные срабатывания, риск убить не тот
процесс. ОБЯЗАТЕЛЬНО фильтровать по имени: `$_.Name -in 'python.exe',
'pythonw.exe'` (вотчдог под pythonw, агент тоже — он наследует sys.executable).
Хелпер `_count_procs()` в agent_watchdog.py.

### Надёжность вотчдога: автозапуск + самовосстановление (2026-06-11)
Вотчдог — единственный процесс, который ОБЯЗАН всегда работать на ПК (телега
не может достучаться до ПК иначе — связь только исходящая ПК→VPS). Сделан
неубиваемым: задача планировщика `RitualB2B_Watchdog` = ДВА триггера:
(1) time-триггер `Once -At <дата> -RepetitionInterval 5min -RepetitionDuration
P3650D` — тикает каждые 5 мин независимо от логона; (2) AtLogOn — мгновенный
старт при входе. ExecutionTimeLimit PT0S (без лимита, иначе Windows убьёт
через 72ч). Запуск `pythonw.exe` (фон, без окна). Дубли исключены:
MultipleInstances=Parallel + `another_watchdog_running()` guard в main()
(планировщик запускает каждые 5 мин, лишний экземпляр сам выходит по guard).
Пересоздание задачи — PowerShell `Register-ScheduledTask` (не schtasks CLI).
E2E-протестировано: убит → воскрешён планировщиком за ~2 сек на ближайшем тике,
ровно 1 живой экземпляр.

⚠️ ГЛАВНАЯ ГРАБЛЯ (инцидент 2026-06-11): первый вариант
`onlogon + $trigger.Repetition = <от Once-триггера>` НЕ работает — `NextRunTime`
получается ПУСТОЙ, будущих запусков нет, убитый вотчдог НЕ воскресает (висел
мёртвым 4 часа). ВСЕГДА проверять после создания задачи:
`(Get-ScheduledTaskInfo -TaskName ...).NextRunTime` — должен быть НЕ пустой.
Надёжный повтор даёт ТОЛЬКО самостоятельный time-триггер (`-Once` с
`-RepetitionInterval`), не repetition пришитый к logon-триггеру.

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

### «disk I/O error» / «database disk image is malformed» у бота (2026-06-12)
Симптом: бот на VPS живой (getUpdates ходит), но фото НЕ ставятся в очередь,
кнопки агента падают; в journalctl — `result_sender упал: disk I/O error` и
`database disk image is malformed`. Диагностика по порядку:
1. `PRAGMA integrity_check` СВЕЖИМ подключением + `df -h`. Если ok и диск
   не полон — это протухший fd долгоживущего соединения в WAL-режиме,
   лечится `systemctl restart ritualb2b-bot ritualb2b-api`.
2. Если integrity_check падает / в таблице появляется мусор (NULL в NOT NULL
   колонках, AUTOINCREMENT начал с 1 при живых старых id, ORDER BY врёт) —
   база реально битая. Лечение (проверено 2026-06-12): остановить bot+api,
   `mv queue.db* → queue.db.corrupt-<дата>*`, создать свежую базу со схемой
   из init_db() (+колонки specs/brand/model), перенести pending-задачи
   (данные вычитать ДО пересборки), запустить сервисы. Старые done-задачи
   не нужны. Файлы фото в input/ при провале уезжают в failed/ с префиксом
   даты — для повторной постановки копировать обратно в input/.

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

## Failover двух генераторов (десктоп + ноутбук, 2026-06-30)

Из-за перебоев с электричеством десктоп («контент-машина») часто выключен.
Добавлен **второй генератор — ноутбук** с авто-failover и приоритетом десктопа.
Спек/план: `docs/superpowers/specs/2026-06-26-dual-worker-failover-design.md`,
`docs/superpowers/plans/2026-06-30-dual-worker-failover.md`. Ветка
`claude/conditioner-approval-pipeline` (запушена).

**Как устроено:**
- VPS API: `POST /api/worker/lease` (worker_id, priority) + таблица `workers`.
  Активен среди свежих (seen_at ≤ `LEASE_TTL_SECONDS`=900) тот, у кого
  наименьший priority. Чистая логика — `worker_lease.py::active_worker_id`.
- Агент (`remote_agent.py`): перед `/api/next-job` зовёт `claim_lease`
  (`worker_lease_client.py`); `active=false` → standby, задачи не берёт.
  Гейт включается только при заданном `WORKER_ID` (без него — active=true,
  обратная совместимость).
- `.env`: `WORKER_ID` + `WORKER_PRIORITY`. Десктоп=1, ноут=2.

**Состояние на 2026-06-30:**
- ✅ VPS-слой развёрнут и проверен (`/root/ritualb2b/vps_api.py` +
  `worker_lease.py`, restart api). Бэкапы `queue.db.bak-*`, `vps_api.py.bak-*`.
- ✅ Ноутбук: `.env` (WORKER_ID=laptop, prio=2, VPS_SSH_KEY=`id_ritualb2b_claude`),
  агент работает, лиз в проде ОК. WatchDog в Планировщике задача
  **`RitualB2B_Watchdog_Laptop`** (отдельное имя от десктопного, триггеры
  5мин+logon, ExecutionTimeLimit=0, NextRunTime непустой) — постоянный.
- ⏳ **Десктоп — НЕ обновлён** (был выключен). Когда включат: `git pull`
  (эта ветка) → в `.env` добавить `WORKER_ID=desktop`,`WORKER_PRIORITY=1` →
  перезапустить агента. ⚠️ ДО этого не запускать оба одновременно: старый
  десктоп-агент лиз не спрашивает → будет конфликт с ноутом. Кнопка «⛔ Стоп
  агента» в боте сейчас управляет ноутом (он единственный поллит команды).
- Грабли env-питона на ноуте: для задачи Планировщика использовать ЯВНЫЙ
  `C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe`
  (а не WindowsApps-алиас).
