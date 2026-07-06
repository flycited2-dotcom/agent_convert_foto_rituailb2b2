# Два генератора с авто-failover — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать ноутбуку работать вторым генератором карточек с авто-failover и приоритетом десктопа — две машины никогда не генерят одновременно.

**Architecture:** Общая очередь на VPS получает лёгкий «лиз воркера»: новый эндпоинт `/api/worker/lease` + таблица `workers`. Чистая логика выбора активного вынесена в отдельные модули (`vps/worker_lease.py` сервер, `worker_lease.py` клиент) — тестируются без FastAPI/Playwright. Агент перед взятием задачи спрашивает лиз и в standby не берёт работу. Всё обратно-совместимо: без `WORKER_ID` агент = active (десктоп работает как раньше).

**Tech Stack:** Python 3, FastAPI (VPS API), httpx (агент), sqlite3, pytest. Деплой на VPS — `ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45` (тот же VPS, root-доступ уже есть).

**Спек:** `docs/superpowers/specs/2026-06-26-dual-worker-failover-design.md`

---

## Файловая структура

- **Create** `vps/worker_lease.py` — чистая логика: `active_worker_id(rows, now, ttl)`. Без зависимостей от FastAPI/sqlite.
- **Modify** `vps/vps_api.py` — +таблица `workers`, +эндпоинт `POST /api/worker/lease`.
- **Create** `worker_lease.py` (корень) — клиентский `claim_lease(client, api_url, token, worker_id, priority)` (async, httpx).
- **Modify** `remote_agent.py` — env `WORKER_ID`/`WORKER_PRIORITY`, лиз-гейт в `agent_loop`.
- **Create** `tests/test_worker_lease.py` — юнит-тесты на серверную логику и клиентский хелпер.
- **Ops** — деплой на VPS, донастройка ноутбука, правка `.env` десктопа, E2E.

---

## Task 1: Серверная логика выбора активного воркера (чистая)

**Files:**
- Create: `vps/worker_lease.py`
- Test: `tests/test_worker_lease.py`

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_worker_lease.py
from __future__ import annotations
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VPS_DIR = _ROOT / "vps"
for _p in (_ROOT, _VPS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from worker_lease import active_worker_id  # vps/worker_lease.py (через _VPS_DIR в sys.path)

NOW = datetime(2026, 6, 30, 12, 0, 0)
TTL = 900  # 15 мин


def _row(wid, prio, seen):
    return {"worker_id": wid, "priority": prio, "seen_at": seen.isoformat()}


def test_desktop_wins_when_both_fresh():
    rows = [_row("desktop", 1, NOW), _row("laptop", 2, NOW)]
    assert active_worker_id(rows, NOW, TTL) == "desktop"


def test_laptop_takes_over_when_desktop_stale():
    rows = [_row("desktop", 1, NOW - timedelta(minutes=20)),
            _row("laptop", 2, NOW)]
    assert active_worker_id(rows, NOW, TTL) == "laptop"


def test_none_fresh_returns_none():
    rows = [_row("desktop", 1, NOW - timedelta(hours=2))]
    assert active_worker_id(rows, NOW, TTL) is None


def test_tiebreak_equal_priority_lexicographic():
    rows = [_row("bbb", 1, NOW), _row("aaa", 1, NOW)]
    assert active_worker_id(rows, NOW, TTL) == "aaa"


def test_bad_seen_at_ignored():
    rows = [_row("laptop", 2, NOW), {"worker_id": "x", "priority": 1, "seen_at": "broken"}]
    assert active_worker_id(rows, NOW, TTL) == "laptop"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/test_worker_lease.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker_lease'`

- [ ] **Step 3: Реализовать `vps/worker_lease.py`**

```python
"""Чистая логика лиза: кто из воркеров сейчас активен. Без FastAPI/sqlite —
принимает строки как данные, тестируется изолированно."""
from __future__ import annotations
import os
from datetime import datetime

LEASE_TTL_SECONDS = int(os.getenv("LEASE_TTL_SECONDS", "900"))  # 15 минут


def active_worker_id(rows, now: datetime, ttl_seconds: int):
    """worker_id активного воркера или None.
    rows: итерируемое из mapping с ключами worker_id, priority, seen_at(iso).
    Активен среди свежих (now - seen_at <= ttl) тот, у кого наименьший priority;
    тай-брейк — наименьший worker_id (детерминированно)."""
    fresh = []
    for r in rows:
        try:
            seen = datetime.fromisoformat(r["seen_at"])
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if (now - seen).total_seconds() <= ttl_seconds:
            fresh.append((int(r["priority"]), str(r["worker_id"])))
    if not fresh:
        return None
    fresh.sort()  # сначала по priority, затем по worker_id
    return fresh[0][1]
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/test_worker_lease.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Коммит**

```bash
git add vps/worker_lease.py tests/test_worker_lease.py
git commit -m "feat(lease): чистая логика выбора активного воркера + тесты"
```

---

## Task 2: Эндпоинт `/api/worker/lease` + таблица `workers`

**Files:**
- Modify: `vps/vps_api.py` (после эндпоинта `agent_command`, ~строка 159)

- [ ] **Step 1: Добавить импорт логики и эндпоинт**

В начало файла, рядом с `from config_vps import ...` (строка 25), добавить:
```python
from worker_lease import LEASE_TTL_SECONDS, active_worker_id
```

После функции `agent_command` (после строки 159) добавить:
```python
@app.post("/api/worker/lease")
def worker_lease(
    x_agent_token: str = Header(...),
    worker_id: str = Form(...),
    priority: int = Form(100),
):
    """Воркер регистрирует heartbeat и спрашивает, активен ли он сейчас.
    active=True → можно брать задачи; False → standby (генерит другой)."""
    _auth(x_agent_token)
    now = datetime.now()
    with db_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workers ("
            "worker_id TEXT PRIMARY KEY, priority INTEGER NOT NULL DEFAULT 100, "
            "seen_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO workers (worker_id, priority, seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET priority=excluded.priority, "
            "seen_at=excluded.seen_at",
            (worker_id, priority, now.isoformat()),
        )
        conn.commit()
        rows = [dict(r) for r in conn.execute(
            "SELECT worker_id, priority, seen_at FROM workers"
        ).fetchall()]
    active_id = active_worker_id(rows, now, LEASE_TTL_SECONDS)
    return {"active": active_id == worker_id, "worker_id": worker_id,
            "active_worker": active_id}
```

- [ ] **Step 2: Тест эндпоинта (temp db + monkeypatch db_conn)**

Добавить в `tests/test_worker_lease.py`:
```python
import sqlite3
import importlib


def _api_with_db(tmp_path, monkeypatch):
    import vps_api
    db = tmp_path / "q.db"
    def _conn():
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row; return c
    monkeypatch.setattr(vps_api, "db_conn", _conn)
    monkeypatch.setattr(vps_api, "API_TOKEN", "")  # отключить проверку токена в тесте
    return vps_api


def test_lease_endpoint_desktop_then_laptop(tmp_path, monkeypatch):
    api = _api_with_db(tmp_path, monkeypatch)
    # десктоп регистрируется первым — активен
    res_d = api.worker_lease(x_agent_token="", worker_id="desktop", priority=1)
    assert res_d["active"] is True
    # ноут с меньшим приоритетом — standby
    res_l = api.worker_lease(x_agent_token="", worker_id="laptop", priority=2)
    assert res_l["active"] is False
    assert res_l["active_worker"] == "desktop"
```

- [ ] **Step 3: Запустить тест эндпоинта**

Run: `pytest tests/test_worker_lease.py -v`
Expected: PASS (6 passed). Если `ImportError: fastapi` — установить: `pip install fastapi`.

- [ ] **Step 4: py_compile проверка синтаксиса**

Run: `python -m py_compile vps/vps_api.py vps/worker_lease.py`
Expected: без ошибок.

- [ ] **Step 5: Коммит**

```bash
git add vps/vps_api.py tests/test_worker_lease.py
git commit -m "feat(lease): эндпоинт /api/worker/lease + таблица workers"
```

---

## Task 3: Клиентский хелпер `claim_lease`

**Files:**
- Create: `worker_lease.py` (корень репозитория)
- Test: `tests/test_worker_lease.py` (дополнить)

- [ ] **Step 1: Написать падающие тесты (async через asyncio.run, без pytest-asyncio)**

Добавить в `tests/test_worker_lease.py`:
```python
import asyncio
import httpx


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")


def test_claim_lease_returns_active_false():
    def handler(req):
        assert req.url.path == "/api/worker/lease"
        return httpx.Response(200, json={"active": False, "active_worker": "desktop"})
    async def go():
        async with _mock_client(handler) as c:
            from claim import claim_lease  # noqa  (см. ниже — корневой worker_lease.py)
            return await claim_lease(c, "http://x", "tok", "laptop", 2)
    assert asyncio.run(go()) is False


def test_claim_lease_empty_worker_id_is_active_no_http():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"active": False})
    async def go():
        async with _mock_client(handler) as c:
            from claim import claim_lease
            return await claim_lease(c, "http://x", "tok", "", 100)
    assert asyncio.run(go()) is True
    assert calls["n"] == 0  # пустой worker_id → HTTP не вызывается


def test_claim_lease_error_defaults_active():
    def handler(req):
        raise httpx.ConnectError("down")
    async def go():
        async with _mock_client(handler) as c:
            from claim import claim_lease
            return await claim_lease(c, "http://x", "tok", "laptop", 2)
    assert asyncio.run(go()) is True  # блип лиза не стопорит воркер
```

> Примечание: корневой модуль называется `worker_lease.py`, но в `sys.path` уже есть `vps/worker_lease.py` (Task 1) под именем `worker_lease`. Чтобы избежать конфликта имён в тестах, импортируем серверную логику через путь `vps`, а клиентскую — дать ОТДЕЛЬНОЕ имя файла: **`worker_lease_client.py`** в корне. Заменить во всех трёх тестах `from claim import claim_lease` на `from worker_lease_client import claim_lease`, и в Task 4 импорт тоже из `worker_lease_client`.

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/test_worker_lease.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker_lease_client'`

- [ ] **Step 3: Реализовать `worker_lease_client.py` (корень)**

```python
"""Клиентский хелпер агента: спросить VPS, активен ли этот воркер (failover-лиз)."""
from __future__ import annotations
import logging

log = logging.getLogger("remote_agent")


async def claim_lease(client, api_url: str, token: str,
                      worker_id: str, priority: int) -> bool:
    """True = можно брать задачи. Пустой worker_id → всегда True (обратная
    совместимость: десктоп без WORKER_ID работает как раньше). Ошибка лиза/сети
    → True (короткий блип не должен стопорить основной воркер)."""
    if not worker_id:
        return True
    try:
        r = await client.post(
            f"{api_url}/api/worker/lease",
            headers={"x-agent-token": token},
            data={"worker_id": worker_id, "priority": priority},
        )
        r.raise_for_status()
        return bool((r.json() or {}).get("active", True))
    except Exception as e:  # noqa: BLE001
        log.warning("lease недоступен (%s) — продолжаю как active.", e)
        return True
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/test_worker_lease.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Коммит**

```bash
git add worker_lease_client.py tests/test_worker_lease.py
git commit -m "feat(lease): клиентский claim_lease + тесты"
```

---

## Task 4: Лиз-гейт в `remote_agent.py`

**Files:**
- Modify: `remote_agent.py` (env-блок ~строки 42-49; импорт ~строка 40; `agent_loop` ~строки 102-117)

- [ ] **Step 1: Добавить env-переменные воркера**

После строки `POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "10"))` (строка 49) добавить:
```python
WORKER_ID       = os.getenv("WORKER_ID", "").strip()
WORKER_PRIORITY = int(os.getenv("WORKER_PRIORITY", "100"))
```

- [ ] **Step 2: Добавить импорт хелпера**

После `from ssh_tunnel import SSHTunnel  # noqa: E402` (строка 40) добавить:
```python
from worker_lease_client import claim_lease  # noqa: E402
```

- [ ] **Step 3: Вставить лиз-гейт в `agent_loop` (после heartbeat, перед проверкой Chrome)**

Между блоком heartbeat (заканчивается на `except Exception: pass`, строка 106) и комментарием `# Chrome мёртв…` (строка 108) вставить:
```python
                # --- Failover-лиз: если активен другой воркер, в standby ---
                if WORKER_ID:
                    active = await claim_lease(client, api_url, VPS_API_TOKEN,
                                               WORKER_ID, WORKER_PRIORITY)
                    if not active:
                        log.info("standby (worker=%s, активен другой) — жду %d сек.",
                                 WORKER_ID, POLL_INTERVAL)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
```

- [ ] **Step 4: Проверить синтаксис**

Run: `python -m py_compile remote_agent.py`
Expected: без ошибок. (Полный импорт remote_agent тянет playwright; ограничиваемся py_compile — логика claim_lease уже покрыта тестами в Task 3.)

- [ ] **Step 5: Коммит**

```bash
git add remote_agent.py
git commit -m "feat(agent): лиз-гейт перед взятием задачи (standby при неактивности)"
```

---

## Task 5: Деплой изменений на VPS (бэкап → выкладка → restart)

**Files:** заливаем `vps/vps_api.py`, `vps/worker_lease.py` в `/root/ritualb2b/` (или туда, где живёт API).

- [ ] **Step 1: Узнать рабочую директорию API на VPS**

Run:
```bash
ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45 'systemctl show ritualb2b-api -p WorkingDirectory,ExecStart --no-pager'
```
Expected: путь (ожидаемо `/root/ritualb2b`).

- [ ] **Step 2: Бэкап queue.db и текущего vps_api.py**

Run:
```bash
ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45 'cd /root/ritualb2b && cp queue.db queue.db.bak-$(date +%Y%m%d_%H%M%S) && cp vps_api.py vps_api.py.bak-$(date +%Y%m%d_%H%M%S) && ls -la queue.db.bak-* vps_api.py.bak-* | tail -2'
```
Expected: бэкапы созданы.

- [ ] **Step 3: Залить два файла (tar+ssh stdin — scp на VPS ненадёжен)**

Run (из корня репозитория агента):
```bash
tar -czf /tmp/lease.tgz vps/vps_api.py vps/worker_lease.py
ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45 'cat > /tmp/lease.tgz' < /tmp/lease.tgz
ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45 'cd /root/ritualb2b && tar -xzf /tmp/lease.tgz --strip-components=1 vps/vps_api.py vps/worker_lease.py && ls -la vps_api.py worker_lease.py'
```
> Проверить, что на VPS `vps_api.py` и `worker_lease.py` лежат рядом (импорт `from worker_lease import ...` это требует). Если API запускается из `/root/ritualb2b/vps/` — корректировать путь распаковки.

- [ ] **Step 4: py_compile + restart + проверка**

Run:
```bash
ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45 'cd /root/ritualb2b && python3 -m py_compile vps_api.py worker_lease.py && systemctl restart ritualb2b-api && sleep 2 && systemctl is-active ritualb2b-api'
```
Expected: `active`.

- [ ] **Step 5: Дымовой запрос лиза с VPS (read-only, токен скрыт)**

Run:
```bash
ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45 'cd /root/ritualb2b && TOK=$(grep "^API_TOKEN=" .env | cut -d= -f2-) && curl -s -X POST http://127.0.0.1:8765/api/worker/lease -H "x-agent-token: $TOK" -d "worker_id=smoke&priority=1"'
```
Expected: JSON `{"active": true, "worker_id": "smoke", "active_worker": "smoke"}`. Затем (необязательно) удалить тестовую запись: `sqlite3 queue.db "DELETE FROM workers WHERE worker_id='smoke'"`.

---

## Task 6: Донастройка ноутбука (этой машины)

- [ ] **Step 1: SSH-туннель к VPS :8765**

Проверить, авторизован ли `~/.ssh/id_ritualb2b_claude` на VPS для проброса порта:
```bash
ssh -i ~/.ssh/id_ritualb2b_claude -o BatchMode=yes -o ConnectTimeout=10 -L 18765:127.0.0.1:8765 -N root@213.109.202.45 &
sleep 3 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18765/ ; kill %1 2>/dev/null
```
- Если туннель встаёт (любой HTTP-код от API, даже 404/403) → ключ годится, прописать его в `.env`.
- Если отказ — создать ключ ноутбука и добавить его pub в `/root/.ssh/authorized_keys` на VPS строкой `restrict,port-forwarding,permitopen="127.0.0.1:8765",command="/bin/false" <pub>` (через `climat_simf_deploy`).

- [ ] **Step 2: Заполнить `.env` ноутбука**

Дописать/выставить в `agent_convert_foto_rituailb2b2/.env`:
```
VPS_SSH_HOST=213.109.202.45
VPS_SSH_USER=root
VPS_SSH_KEY=<абсолютный путь к рабочему ключу из Step 1>
VPS_SSH_PASS=
VPS_API_PORT=8765
VPS_API_TOKEN=<API_TOKEN с VPS /root/ritualb2b/.env — взять server-side, не эхоить>
WORKER_ID=laptop
WORKER_PRIORITY=2
```

- [ ] **Step 3: Зависимости агента**

Run: `pip install -r requirements.txt` (затем при необходимости `playwright install chromium`, если агент использует не системный Chrome).
Expected: установлено без ошибок.

- [ ] **Step 4: Проверить Chrome + ChatGPT-логин**

Run: `start_chrome.bat`, открыть в нём `CONDITIONER_PROJECT_URL` из `.env` — убедиться, что аккаунт ChatGPT залогинен и проект доступен. (CDP проверка: `curl http://127.0.0.1:9333/json/version`.)

- [ ] **Step 5: Разовый прогон агента (ручной, без вотчдога)**

Run: `python remote_agent.py` (Chrome уже открыт). В логе ожидать «Агент запущен» и либо обработку задачи, либо `204`/standby. Прервать Ctrl+C после проверки.
Expected: туннель встаёт, лиз отвечает, при наличии задачи — генерация.

- [ ] **Step 6: WatchDog в Task Scheduler ноутбука**

Зарегистрировать задачу `RitualB2B_Watchdog_Laptop` (имя отличное от десктопного) по образцу `agent_watchdog.py`/их CLAUDE.md (time-триггер каждые 5 мин + AtLogOn, pythonw). Вотчдог держит агента живым; агент сам гейтится лизом. Проверить `(Get-ScheduledTaskInfo).NextRunTime` непустой.

---

## Task 7: Подключить десктоп к схеме (последним)

- [ ] **Step 1: Дописать в `.env` десктопа**

```
WORKER_ID=desktop
WORKER_PRIORITY=1
```
- [ ] **Step 2: Перезапустить агента на десктопе** (через кнопку «🔁 Перезапуск агента» в боте или вотчдог). До этой правки десктоп работал по дефолту (active=true) — регресса нет.

---

## Task 8: E2E-проверка failover (ручная)

- [ ] **Step 1:** Обе машины онлайн (агенты запущены) → по логам убедиться: десктоп берёт задачи, ноут пишет «standby».
- [ ] **Step 2:** Выключить/погасить агента десктопа → в течение ≤15 мин ноут переходит в active и начинает брать задачи (лог ноута). Проверить `workers.seen_at` на VPS.
- [ ] **Step 3:** Вернуть десктоп → ноут доводит текущую карточку до конца, затем уходит в standby; десктоп снова active.
- [ ] **Step 4:** Зафиксировать результат и обновить память проекта агента (CLAUDE.md) парой строк про failover.

---

## Self-review (выполнено при написании)

- **Покрытие спека:** лиз+приоритет (Task 1-2), клиент-гейт (Task 3-4), деплой (Task 5), ноут-сетап вкл. SSH/Chrome/WatchDog (Task 6), десктоп env (Task 7), E2E+хендбэк (Task 8). TTL=900/приоритеты/обратная совместимость отражены.
- **Заглушки:** нет — весь код приведён.
- **Согласованность имён:** серверная логика `vps/worker_lease.py::active_worker_id`; клиент — отдельный файл `worker_lease_client.py::claim_lease` (во избежание конфликта имён модулей в `sys.path`); агент импортирует из `worker_lease_client`. Эндпоинт `/api/worker/lease`, поля формы `worker_id`/`priority`, ответ `active`/`active_worker`.
