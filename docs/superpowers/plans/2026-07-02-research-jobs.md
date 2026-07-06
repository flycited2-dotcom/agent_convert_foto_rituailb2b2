# Research-задачи («наименование → фото + УТП») — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Новый тип задачи в очереди фотоагента: по бренду/модели/категории товара ChatGPT
(проект с веб-поиском) находит УТП и делает изображение товара; результат (фото + текст УТП)
забирает content-factory для карточек и подписей (Excel-источник, холодильники и т.п.).

**Architecture:** Тот же конвейер, что у карточек: очередь jobs в queue.db на VPS,
`mode='research'`, вход БЕЗ фото (`input_filename=''`), выход — файл в OUTPUT_DIR + новая
колонка `jobs.result_specs` (текст УТП). Агент ветвится по mode ДО проверки input_filename.
`result_sender` бота research-задачи не трогает (их забирает content-factory).
Спека: `Codex/content-factory/docs/superpowers/specs/2026-07-02-full-automation-design.md` (подпроект 2).

**Tech Stack:** FastAPI (vps_api), python-telegram-bot (vps_bot), Playwright/CDP (agent),
sqlite3, unittest (tests/ без сети). Деплой VPS — paramiko/base64-чанки (SFTP нестабилен).

---

### Task 1: Схема БД — колонка `result_specs` + result_sender не трогает research

**Files:**
- Modify: `vps/vps_bot.py` (init_db — миграция; result_sender — фильтр)

- [ ] **Step 1: Миграция** — в `init_db()` в цикл `for ddl in (...)` добавить строку:

```python
            "ALTER TABLE jobs ADD COLUMN result_specs TEXT",
```

- [ ] **Step 2: Фильтр result_sender** — в фоновой задаче result_sender оба SELECT дополнить
  `AND mode != 'research'`:

```python
                done_rows   = conn.execute(
                    "SELECT * FROM jobs WHERE status='done'   AND result_sent=0 "
                    "AND mode != 'research' ORDER BY id"
                ).fetchall()
                failed_rows = conn.execute(
                    "SELECT * FROM jobs WHERE status='failed' AND result_sent=0 "
                    "AND mode != 'research' ORDER BY id"
                ).fetchall()
```

  (research-результаты и research-ошибки забирает/алертит content-factory сам.)
- [ ] **Step 3: Коммит** — `git commit -m "feat(vps): result_specs + research-задачи мимо result_sender"`

### Task 2: VPS API — submit-research / complete-research

**Files:**
- Modify: `vps/vps_api.py`
- Test: `tests/test_vps_api_research.py` (fastapi TestClient + tmp queue.db)

- [ ] **Step 1: Падающий тест** (`tests/test_vps_api_research.py`; перед импортом vps_api
  подменить env/config_vps на tmp-пути — по образцу существующих тестов repo, если их нет:
  monkeypatch `config_vps.DB_PATH` и директории):

```python
import io
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class ResearchApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        for d in ("input", "output", "processed", "failed"):
            (root / d).mkdir()
        import sys
        sys.path.insert(0, "vps")
        import config_vps
        config_vps.DB_PATH = root / "queue.db"
        config_vps.INPUT_DIR = root / "input"
        config_vps.OUTPUT_DIR = root / "output"
        config_vps.PROCESSED_DIR = root / "processed"
        config_vps.FAILED_DIR = root / "failed"
        config_vps.API_TOKEN = "T"
        import importlib
        import vps_api
        importlib.reload(vps_api)
        con = sqlite3.connect(config_vps.DB_PATH)
        con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, "
                    "input_filename TEXT, status TEXT DEFAULT 'pending', mode TEXT, specs TEXT, "
                    "brand TEXT, model TEXT, caption TEXT, output_filename TEXT, "
                    "archived_filename TEXT, failed_filename TEXT, error_text TEXT, "
                    "result_sent INTEGER DEFAULT 0, result_specs TEXT, "
                    "created_at TEXT, updated_at TEXT)")
        con.commit(); con.close()
        from fastapi.testclient import TestClient
        self.client = TestClient(vps_api.app)
        self.db = config_vps.DB_PATH

    def tearDown(self):
        self.tmp.cleanup()

    def test_submit_research_creates_pending_job(self):
        r = self.client.post("/api/submit-research", headers={"x-agent-token": "T"},
                             data={"brand": "Beko", "model": "B1RCSK362S",
                                   "category": "холодильник", "chat_id": "42"})
        self.assertEqual(r.status_code, 200)
        job_id = r.json()["job_id"]
        row = sqlite3.connect(self.db).execute(
            "SELECT mode, brand, model, specs, status, input_filename FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        self.assertEqual(row, ("research", "Beko", "B1RCSK362S", "холодильник", "pending", ""))

    def test_complete_research_saves_photo_and_utp(self):
        r = self.client.post("/api/submit-research", headers={"x-agent-token": "T"},
                             data={"brand": "Beko", "model": "X", "category": "х-к",
                                   "chat_id": "42"})
        job_id = r.json()["job_id"]
        r2 = self.client.post(f"/api/complete-research/{job_id}",
                              headers={"x-agent-token": "T"},
                              data={"utp": "✓ No Frost\n✓ Тихий 39 дБ"},
                              files={"photo": ("beko.png", io.BytesIO(b"PNG"), "image/png")})
        self.assertEqual(r2.status_code, 200)
        row = sqlite3.connect(self.db).execute(
            "SELECT status, result_specs, output_filename, result_sent FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        self.assertEqual(row[0], "done")
        self.assertIn("No Frost", row[1])
        self.assertTrue(row[2] and row[3] == 1)

    def test_complete_research_without_photo(self):
        r = self.client.post("/api/submit-research", headers={"x-agent-token": "T"},
                             data={"brand": "B", "model": "M", "category": "c", "chat_id": "1"})
        job_id = r.json()["job_id"]
        r2 = self.client.post(f"/api/complete-research/{job_id}",
                              headers={"x-agent-token": "T"}, data={"utp": "✓ x"})
        self.assertEqual(r2.status_code, 200)
        row = sqlite3.connect(self.db).execute(
            "SELECT status, output_filename FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row, ("done", None))     # фото нет → карточку потом «по названию»


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что падает** — `python -m unittest tests.test_vps_api_research -v` → FAIL (404, нет эндпоинтов)
- [ ] **Step 3: Реализация** в `vps/vps_api.py` (после submit_job):

```python
@app.post("/api/submit-research")
async def submit_research(
    x_agent_token: str = Header(...),
    brand: str = Form(""),
    model: str = Form(...),
    category: str = Form(""),
    chat_id: int = Form(...),
):
    """content-factory ставит research-задачу: по наименованию найти фото + УТП.
    Входного фото НЕТ (input_filename='') — агент ветвится по mode='research'.
    category кладём в specs (промпту агента нужен тип товара)."""
    _auth(x_agent_token)
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (chat_id, input_filename, mode, specs, brand, model) "
            "VALUES (?, '', 'research', ?, ?, ?)",
            (chat_id, category or None, brand or None, model),
        )
        conn.commit()
        job_id = cur.lastrowid
    log.info("submit-research: brand=%s model=%s → job %d", brand, model, job_id)
    return {"ok": True, "job_id": job_id}


@app.post("/api/complete-research/{job_id}")
async def complete_research(
    job_id: int,
    x_agent_token: str = Header(...),
    utp: str = Form(""),
    photo: UploadFile | None = File(None),
):
    """Агент возвращает результат research: текст УТП + (опционально) фото товара.
    result_sent=1 сразу — result_sender бота эти задачи не рассылает."""
    _auth(x_agent_token)
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    out_filename = None
    if photo is not None:
        ext = Path(photo.filename or "r.png").suffix.lower() or ".png"
        out_filename = f"research_{job_id}{ext}"
        (OUTPUT_DIR / out_filename).write_bytes(await photo.read())
    with db_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', output_filename=?, result_specs=?, "
            "result_sent=1, updated_at=? WHERE id=?",
            (out_filename, utp or None, datetime.now().isoformat(), job_id),
        )
        conn.commit()
    log.info("Research %d done: фото=%s, УТП=%d симв.", job_id, out_filename, len(utp or ""))
    return {"ok": True, "output": out_filename}
```

- [ ] **Step 4: Тест зелёный** — `python -m unittest tests.test_vps_api_research -v` → OK
- [ ] **Step 5: Коммит** — `git commit -m "feat(api): submit-research / complete-research (фото+УТП по наименованию)"`

### Task 3: Режим research в config.py (без эталонов)

**Files:**
- Modify: `config.py`
- Create: `prompts/research.txt`
- Test: `tests/test_research_mode.py`

- [ ] **Step 1: Падающий тест**

```python
import unittest
from config import get_mode


class ResearchModeTest(unittest.TestCase):
    def test_research_mode_exists_and_needs_no_reference(self):
        m = get_mode("research")
        self.assertEqual(m.key, "research")
        self.assertFalse(m.requires_reference)

    def test_render_prompt_substitutes_product(self):
        m = get_mode("research")
        p = m.render_prompt("Холодильник Beko B1RCSK362S")
        self.assertIn("Beko B1RCSK362S", p)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что падает** — `python -m unittest tests.test_research_mode -v` → FAIL (get_mode вернёт default)
- [ ] **Step 3: Реализация** в `config.py`:
  - в dataclass `Mode` добавить поле `requires_reference: bool = True`;
  - в `Mode.is_configured` заменить последнюю строку на:

```python
        if self.requires_reference:
            return any(f.exists() for f in self.reference_files) and bool(self.reference_files)
        return True
```

  - в `MODES` добавить:

```python
    "research": Mode(
        key="research",
        label="🔎 Research (фото+УТП по названию)",
        project_url=os.getenv("RESEARCH_PROJECT_URL", "").strip(),
        reference_files=[],
        prompt=_mode_prompt("research", "RESEARCH_PROMPT"),
        enabled=True,
        requires_specs=True,          # {{SPECS}} = «Категория Бренд Модель»
        requires_reference=False,
    ),
```

- [ ] **Step 4: Промпт** `prompts/research.txt` (первая версия, донастроится на пилоте):

```
Найди в интернете товар: {{SPECS}}.

Сделай две вещи:

1. Выпиши ровно 5–7 главных потребительских преимуществ (УТП) этой модели
   по-русски, каждое до 40 символов, строками вида «✓ …». Только реальные
   характеристики из найденных источников, без выдумок и без маркетинговой воды.
   Никакого другого текста вокруг списка.

2. Сгенерируй ОДНО фотореалистичное изображение именно этой модели на чистом
   белом фоне (студийная предметная съёмка, весь товар в кадре, без текста,
   без логотипов магазинов и водяных знаков), максимально похожее на найденные
   официальные фото товара.
```

- [ ] **Step 5: Тест зелёный + коммит** — `git commit -m "feat(config): режим research (без эталонов) + промпт"`

### Task 4: agent.py — process_research (текст УТП + картинка из ответа)

**Files:**
- Modify: `agent.py`
- Test: `tests/test_parse_utp.py` (чистый парсер; браузерная часть проверяется пилотом)

- [ ] **Step 1: Падающий тест парсера УТП**

```python
import unittest
from agent import parse_utp_lines


class ParseUtpTest(unittest.TestCase):
    def test_extracts_check_lines(self):
        text = "Вот преимущества:\n✓ No Frost\n✓ Тихий 39 дБ\nНадеюсь, полезно!"
        self.assertEqual(parse_utp_lines(text), ["✓ No Frost", "✓ Тихий 39 дБ"])

    def test_normalizes_bullets_and_dashes(self):
        text = "- No Frost\n• Инверторный компрессор\n1. Класс A++"
        self.assertEqual(parse_utp_lines(text),
                         ["✓ No Frost", "✓ Инверторный компрессор", "✓ Класс A++"])

    def test_empty_when_no_list(self):
        self.assertEqual(parse_utp_lines("просто текст без списка"), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что падает** → FAIL (нет parse_utp_lines)
- [ ] **Step 3: Реализация** в `agent.py`:

```python
import re

_UTP_LINE = re.compile(r"^\s*(?:[✓✔•\-–*]|\d+[.)])\s+(.{3,60})\s*$")


def parse_utp_lines(text: str, max_items: int = 7) -> list[str]:
    """Строки-УТП из ответа ChatGPT: маркированные (✓ - • цифры) → «✓ …»."""
    out = []
    for line in (text or "").splitlines():
        m = _UTP_LINE.match(line)
        if m:
            out.append(f"✓ {m.group(1).strip()}")
        if len(out) >= max_items:
            break
    return out


LAST_ASSISTANT_TEXT_JS = """
    () => {
        const sel = '[data-message-author-role="assistant"], [data-author-role="assistant"]';
        const msgs = [...document.querySelectorAll(sel)];
        return msgs.length ? msgs[msgs.length - 1].innerText : '';
    }
"""


async def process_research(brand: str | None, model: str | None,
                           category: str | None) -> tuple[Path | None, str]:
    """Research-задача: без входного фото. Открывает research-проект, спрашивает
    УТП + изображение товара. Возвращает (путь к картинке | None, текст УТП).
    Ошибка = исключение (ретраи делает remote_agent, как у карточек)."""
    cfg = get_mode("research")
    if not cfg.is_configured:
        raise RuntimeError("Режим 'research' не настроен: нет RESEARCH_PROJECT_URL/промпта")
    product = " ".join(x for x in (category, brand, model) if x).strip()
    prompt = cfg.render_prompt(product)
    output_path = make_output_path(mode="research", brand=brand, model=model)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CHROME_CDP_URL)
        try:
            page = await find_or_open_chatgpt(browser)
            await open_new_chat(page, url=cfg.project_url or "https://chatgpt.com/")
            await paste_text(page, prompt)
            await submit(page)
            await asyncio.sleep(5)
            baseline_srcs = await snapshot_image_srcs(page)

            photo: Path | None = None
            try:
                await wait_for_generation(page, GENERATION_TIMEOUT_SEC * 1000, baseline_srcs)
                await download_via_anchor(page, output_path, baseline_srcs)
                photo = output_path
            except Exception as e:          # фолбэк спеки: фото нет → карточка «по названию»
                log.warning("research: изображение не получено (%s) — вернём только УТП", e)

            text = await page.evaluate(LAST_ASSISTANT_TEXT_JS)
            utp = parse_utp_lines(text)
            if not utp:
                raise RuntimeError("research: в ответе не найден список УТП")
            return photo, "\n".join(utp)
        finally:
            await browser.close()
```

  Примечание: `make_output_path` использует `_MODE_FILE_PREFIX.get(mode, mode)` — для
  research добавить в `_MODE_FILE_PREFIX` пару `"research": "research"`.
- [ ] **Step 4: Тест зелёный + коммит** — `git commit -m "feat(agent): process_research — УТП + изображение по наименованию"`

### Task 5: remote_agent.py — ветка research в цикле

**Files:**
- Modify: `remote_agent.py`

- [ ] **Step 1: Реализация** — в `agent_loop` сразу после получения `job` (ДО проверки
  `if not input_filename:` — у research input_filename пустой, это НЕ битая задача):

```python
                if job_mode == "research":
                    from agent import process_research
                    last_error = None
                    for attempt in range(1, 4):
                        try:
                            if attempt > 1:
                                log.warning("research: попытка %d/3 для задачи %d…", attempt, job_id)
                                await asyncio.sleep(15)
                            photo, utp = await process_research(
                                job_brand, job_model, job.get("specs") or None)
                            files = {}
                            if photo and photo.exists():
                                files = {"photo": (photo.name, photo.read_bytes(), "image/png")}
                            r = await client.post(
                                f"{api_url}/api/complete-research/{job_id}",
                                headers=headers, data={"utp": utp}, files=files or None,
                                timeout=120)
                            r.raise_for_status()
                            if photo:
                                photo.unlink(missing_ok=True)
                            last_error = None
                            break
                        except Exception as e:
                            last_error = e
                            log.warning("research: попытка %d/3 не удалась: %s", attempt, e)
                    if last_error is not None:
                        log.error("research-задача %d провалилась: %s", job_id, last_error)
                        try:
                            await client.post(f"{api_url}/api/fail/{job_id}",
                                              headers=headers, data={"error": str(last_error)})
                        except Exception:
                            pass
                    await asyncio.sleep(DELAY_BETWEEN_JOBS_SEC)
                    continue
```

- [ ] **Step 2: Смоук-импорт** — `python -c "import remote_agent"` (без Chrome просто импорт) → без ошибок
- [ ] **Step 3: Коммит** — `git commit -m "feat(remote_agent): обработка research-задач (без входного фото)"`

### Task 6: Деплой + пилот (внешнее, по ОК владельца)

- [ ] **Step 1 (владелец):** создать ChatGPT-проект «Research» с включённым веб-поиском;
  URL проекта → `.env` ноута (и потом десктопа): `RESEARCH_PROJECT_URL=...`.
- [ ] **Step 2:** VPS: залить `vps/vps_bot.py`, `vps/vps_api.py` (paramiko/base64-чанки,
  py_compile), `systemctl restart ritualb2b-bot ritualb2b-api`. Бэкап queue.db перед миграцией.
- [ ] **Step 3:** Ноут: `git pull`, перезапуск агента (вотчдог/кнопка «Перезапуск»).
- [ ] **Step 4: Пилот:** с VPS `curl -X POST http://127.0.0.1:8765/api/submit-research
  -H "x-agent-token: $TOKEN" -d brand=Beko -d model=B1RCSK362S -d category=холодильник
  -d chat_id=1264067528` → дождаться status='done' в queue.db, проверить `result_specs`
  и файл `output/research_*.png`. Донастроить промпт по качеству результата.

---

## Интеграция в content-factory (подпроект 3, отдельный план)

`research_pipeline.py` (аналог cards_pipeline): ResearchStore (key→job_id), submit через
`/api/submit-research`, сбор — read-only queue.db (`SELECT status, output_filename,
result_specs FROM jobs WHERE id IN (...)`) + копия фото из OUTPUT_DIR. Будет описан
в плане Excel-источника.
