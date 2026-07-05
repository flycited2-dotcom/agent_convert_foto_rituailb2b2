> **Спека дизайна (утверждено 2026-07-06).** Копия в обоих репо:
> реализация — `agent_convert_foto_rituailb2b2` (этот файл), координация/ops —
> `Content_factory` (`docs/superpowers/specs/` + `docs/HANDOFF-MULTI-MACHINE.md`).

# План: несколько ChatGPT-аккаунтов с перехватом при лимите (параллельные дорожки)

## Context

Фотоагент генерит карточки, гоняя веб-ChatGPT через Chrome CDP. Сейчас цепочка
**одноканальная**: один Chrome (профиль `chrome_profile/`, порт 9333), один аккаунт,
один `remote_agent.py`. Когда аккаунт упирается в лимит генерации картинок, работа
встаёт (модалка лимита просто ломает клики — graceful-обработки нет). У владельца есть
второй ChatGPT-аккаунт, которым он хочет **автоматически подхватывать** работу, а в идеале
молотить очередь **параллельно** (быстрее).

Цель: ввести понятие **дорожки** (lane = профиль Chrome + порт + аккаунт). Несколько дорожек
на одной или разных машинах тянут **одну центральную очередь на VPS**; атомарный claim не даёт
дублей; дорожка на лимите «остывает» и молчит, остальные продолжают — перехват получается сам.

Вся работа — в репозитории фотоагента
`C:\Users\TLT-1\Documents\GitHub\agent_convert_foto_rituailb2b2_V2_office\agent_convert_foto_rituailb2b2`
(далее «репо агента»). `content-factory` только ставит задачи и читает статус — правки там
минимальны (см. Phase 6).

Подход — **один процесс `remote_agent.py` на дорожку** (переиспользуем текущий агент почти как
есть; изоляция сбоев: умер один Chrome — падает только его дорожка; параллель и кросс-машинный
перехват «бесплатно» за счёт центральной очереди).

## Ключевые факты кодовой базы (уже проверено)

- `vps/vps_api.py` `/api/next-job` — **claim НЕ атомарен**: `SELECT ... status='pending' ORDER BY id
  LIMIT 1`, затем отдельный `UPDATE ... status='processing'`. Две дорожки одновременно схватят одну
  задачу. Фундамент фичи.
- `config.py` — `CHROME_CDP_URL` (один, `http://127.0.0.1:9333`); `Mode` = ChatGPT-**проект** внутри
  аккаунта (`project_url`). У второго аккаунта проекты свои → нужны per-lane `project_url`.
- `agent.py` `process_one_file(input, mode, specs, brand, model)` — берёт `CHROME_CDP_URL` и
  `get_mode(mode).project_url` из модуля. **Детекта лимита нет** (grep пусто).
- `remote_agent.py` — SSH-туннель к VPS, один цикл: heartbeat → проверка живости Chrome CDP →
  `/api/next-job` → скачать вход → `process_one_file` (3 попытки) → `/api/complete|fail`. Один
  `CHROME_CDP_URL`.
- `agent_watchdog.py` — поллит `/api/agent-command` (глобальный флаг), start/stop/restart,
  самовосстановление; `agent_state.txt` — желаемое состояние; kill Chrome по порту в cmdline,
  kill агента по подстроке `remote_agent`.
- `vps_api.py`: `agent_command` — один глобальный флаг; `agent_heartbeat` — одна строка `id=1`.
- `start_chrome.bat` — порт 9333 и профиль зашиты константами.

## Предпосылки (ручное, вне кода — сделать владельцу)

1. **Аккаунт 2 в ChatGPT:** создать те же проекты, что на акк1 (по одному на активный `Mode`),
   загрузить в них те же эталоны; записать `project_url` каждого проекта — пойдут в конфиг дорожки.
2. **Отдельный профиль Chrome для акк2:** однократный логин (`start_chrome.bat 9334 chrome_profile_acc2`
   после Phase 2 → войти в аккаунт 2, сессия сохранится).

## Phase 0 — Снять разметку модалки лимита (blocker для детекта)

Нельзя писать детект вслепую. Дождаться/спровоцировать лимит на акк1 и сохранить DOM+screenshot
состояния «лимит достигнут» (переиспользовать `agent._dump_page_state` — он уже пишет `.png`+`.html`
в logs при таймауте; вызвать его в этот момент). Зафиксировать: селектор/текст модалки
(«you've reached your limit…», «limit resets at…») и формат времени сброса.
Итог фазы — фикстура `tests/fixtures/rate_limit_modal.html` для юнит-теста детекта.

## Phase 1 — Атомарный claim + аренда в VPS-API (безопасно и при 1 дорожке)

`vps/vps_api.py`:
- Миграция схемы `jobs`: добавить `claimed_by TEXT`, `claimed_at TEXT` (idempotent `ALTER TABLE`
  в старте приложения, как уже делается `CREATE TABLE IF NOT EXISTS flags`).
- `/api/next-job`: атомарный claim одним запросом (SQLite 3.35+ `UPDATE ... RETURNING`):
  `UPDATE jobs SET status='processing', claimed_by=:lane, claimed_at=:now
   WHERE id=(SELECT id FROM jobs WHERE status='pending' ORDER BY id LIMIT 1) RETURNING *;`
  Принимать `lane` из query (`?lane=<id>`) или заголовка.
- **Аренда (lease) от смерти дорожки:** в начале `next-job` реквеуить зависшие —
  `UPDATE jobs SET status='pending', claimed_by=NULL WHERE status='processing' AND claimed_at < :now-LEASE`
  (LEASE, напр. 15 мин из env). Так задача павшей дорожки вернётся в общий пул.

Проверка: юнит-тест — N pending, 2 потока параллельно бьют claim-функцию → каждая задача выдана
ровно раз (см. Verification).

## Phase 2 — Параметризовать запуск Chrome (N профилей/портов)

- `start_chrome.bat` → принимать `%1`=порт, `%2`=имя профиля (дефолты 9333/`chrome_profile` — обратная
  совместимость). Профиль акк2 — отдельный `chrome_profile_acc2/` (Chrome требует разные `user-data-dir`).
- Логин акк2 — из предпосылок.

## Phase 3 — Ввести дорожки в агента

- **Конфиг дорожек** — новый `lanes.json` в корне репо агента (stdlib `json`, без новых зависимостей):
  список `{id, label, cdp_port, profile_dir, enabled, project_urls:{mode:url}}`. `project_urls`
  переопределяет `Mode.project_url` для дорожки; пусто → фоллбэк на текущий `Mode.project_url`.
  Загрузчик `load_lanes()` + `get_lane(id)` — в `config.py` рядом с `get_mode`.
- `agent.py` `process_one_file(...)`: добавить необязательные `cdp_url: str | None`,
  `project_url: str | None`; дефолт — текущие модульные значения (обратная совместимость). Использовать
  их в `connect_over_cdp` и при выборе `chat_url`.
- `remote_agent.py`: читать `LANE_ID` (env/CLI-арг); резолвить дорожку из `lanes.json` →
  свой `cdp_url=http://127.0.0.1:{cdp_port}` и per-mode `project_url`; отдельный лог
  `logs/remote_agent_{lane}.log`; передавать `?lane={id}` в `/api/next-job` и heartbeat.
- `vps_api.py` heartbeat: ключ по дорожке (PK `lane` вместо фиксированного `id=1`), чтобы видеть
  каждую дорожку отдельно.

## Phase 4 — Детект лимита + остывание/реквеуе

- `agent.py`: `detect_rate_limit(page) -> datetime | None` (по разметке из Phase 0) + класс
  `RateLimitError(reset_at)`. Вызывать в `wait_for_generation` и после `submit`; при лимите —
  `raise RateLimitError`.
- `vps_api.py`: `POST /api/requeue/{job_id}` → `status='pending', claimed_by=NULL` (мягкий возврат,
  НЕ failed — не жжём попытку).
- `remote_agent.py`: ловить `RateLimitError` отдельно от прочих: реквеуить текущую задачу
  (`/api/requeue`), затем спать до `reset_at` (с потолком из env, напр. 90 мин), потом продолжать.
  Остальные дорожки в это время добирают очередь — это и есть перехват.

## Phase 5 — Вотчдог на N дорожек + пофлаговый контроль

- `agent_watchdog.py`: читать `lanes.json`; для каждой `enabled`-дорожки держать её Chrome (свой порт+
  профиль, параметризованный `start_chrome.bat`) и свой `remote_agent.py` с `LANE_ID`. Per-lane
  желаемое состояние `agent_state_{lane}.txt`. `_count_procs/kill_chrome/kill_agent` — по порту дорожки
  и по `LANE_ID` в cmdline.
- **Пофлаговые команды:** `vps_api.py` — команды по дорожке: `flags` ключ `agent_command:{lane}`
  (или таблица `lane_commands`); `/api/agent-command?lane={id}` отдаёт+сбрасывает команду этой дорожки.
- `vps/vps_bot.py` (70 КБ — прочитать при реализации): кнопки start/stop/restart **на каждую дорожку**
  + статус по per-lane heartbeat. Ставит per-lane флаг вместо глобального.

## Phase 6 — Согласовать content-factory и документация

- `content-factory/src/content_factory/cards_pipeline.py::wake_agent` ставит **глобальный**
  `agent_command='start'`. Оставить как «поднять все дорожки» (вотчдог трактует как start всех
  `enabled`), либо убрать — уточнить при реализации.
- **Реконсиляция с `machines.yaml`:** атомарная очередь (Phase 1) снимает нужду в «одна машина тянет».
  `command_key` в `config/machines.yaml` перепрофилировать под набор дорожек машины/адресацию
  start-stop, а не под эксклюзивность. Обновить `docs/HANDOFF-MULTI-MACHINE.md`.
- Обновить `README.md`/`ИНСТРУКЦИЯ.txt` репо агента (как добавить дорожку/аккаунт) и хендофф.

## Verification

- **Атомарность (Phase 1):** юнит-тест в `tests/` репо агента — временная sqlite с N pending, 2 потока
  параллельно вызывают claim-функцию, собрать выданные id → нет повторов, выдано min(N, вызовов).
- **Реквеуе-аренда:** тест — задача `processing` с `claimed_at` в прошлом → следующий `next-job`
  возвращает её в `pending`.
- **Детект лимита (Phase 4):** юнит-тест на фикстуре `tests/fixtures/rate_limit_modal.html` →
  `detect_rate_limit` возвращает время сброса; на обычной странице → `None`.
- **E2E локально:** поднять 2 дорожки (акк1:9333, акк2:9334) на тестовую очередь VPS → обе тянут
  РАЗНЫЕ задачи параллельно (по логам `remote_agent_{lane}.log`). Затем спровоцировать/сымитировать
  лимит на одной → её задача уходит в `pending`, дорожка спит, вторая добирает очередь; после сброса
  первая возвращается.
- Прогнать `pytest` репо агента — зелёный (правила из его `CLAUDE.md`: TDD, тесты первыми).

## Порядок и риски

Фазы идут по возрастанию риска и каждая самостоятельно ценна: 0→1 можно катить, не трогая рантайм
дорожек (claim безопасен и для одной). 2–3 включают вторую дорожку. 4 добавляет собственно перехват.
5 — контроль из Telegram. 6 — уборка.
Главный внешний риск — **ToS/детект** при параллели двух аккаунтов; `max_pending`/задержки уже есть в
`cards_pipeline`/`config`, параллель усиливает нагрузку — держать консервативные тайминги.
