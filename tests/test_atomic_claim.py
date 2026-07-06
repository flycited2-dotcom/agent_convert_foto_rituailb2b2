"""Атомарный claim задач в /api/next-job (Phase 1 мульти-дорожек, спека
2026-07-06-multi-account-chatgpt-lanes): выбор и захват одним UPDATE…RETURNING —
две дорожки не получат одну задачу; аренда (lease) возвращает в пул задачи
умерших дорожек; caps-фильтр (research) сохранён; колонки claimed_by/claimed_at
мигрируются идемпотентно на старой схеме. Схема в setUp — НАМЕРЕННО старая
(без claim-колонок): каждый тест прогоняет миграцию."""
import importlib
import os
import sqlite3
import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vps"))


class AtomicClaimTest(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: vps_api держит sqlite-соединения открытыми
        # (with = транзакция, не close) — Windows не даёт удалить queue.db сразу.
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        for d in ("input", "output", "processed", "failed", "logs"):
            os.environ[f"{d.upper()}_DIR"] = str(root / d)
        os.environ["API_TOKEN"] = "T"
        import config_vps
        importlib.reload(config_vps)
        config_vps.DB_PATH = root / "queue.db"
        import vps_api
        importlib.reload(vps_api)
        con = sqlite3.connect(config_vps.DB_PATH)
        con.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, "
            "input_filename TEXT, status TEXT DEFAULT 'pending', mode TEXT, specs TEXT, "
            "brand TEXT, model TEXT, caption TEXT, output_filename TEXT, "
            "archived_filename TEXT, failed_filename TEXT, error_text TEXT, "
            "result_sent INTEGER DEFAULT 0, result_specs TEXT, "
            "created_at TEXT, updated_at TEXT)")
        con.commit()
        con.close()
        from fastapi.testclient import TestClient
        self.client = TestClient(vps_api.app)
        self.db = config_vps.DB_PATH

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, n=1, status="pending", mode="ritual"):
        con = sqlite3.connect(self.db)
        ids = [con.execute(
            "INSERT INTO jobs (input_filename, status, mode) VALUES (?, ?, ?)",
            (f"f{i}.png", status, mode)).lastrowid for i in range(n)]
        con.commit()
        con.close()
        return ids

    def _set_claim(self, job_id, claimed_by, claimed_at):
        """Проставить claim-поля напрямую (для тестов аренды)."""
        con = sqlite3.connect(self.db)
        for ddl in ("ALTER TABLE jobs ADD COLUMN claimed_by TEXT",
                    "ALTER TABLE jobs ADD COLUMN claimed_at TEXT"):
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
        con.execute("UPDATE jobs SET claimed_by=?, claimed_at=? WHERE id=?",
                    (claimed_by, claimed_at, job_id))
        con.commit()
        con.close()

    def _next(self, lane="", caps=""):
        params = {}
        if lane:
            params["lane"] = lane
        if caps:
            params["caps"] = caps
        return self.client.get("/api/next-job",
                               headers={"x-agent-token": "T"}, params=params)

    def _row(self, job_id):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        con.close()
        return row

    # ── миграция + захват ────────────────────────────────────────────────────
    def test_claim_on_legacy_schema_sets_lane_and_time(self):
        (jid,) = self._seed(1)
        r = self._next(lane="laptop-a1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], jid)
        row = self._row(jid)
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["claimed_by"], "laptop-a1")
        self.assertTrue(row["claimed_at"])

    def test_claim_without_lane_still_works(self):
        # текущий remote_agent параметр lane не шлёт — обратная совместимость
        (jid,) = self._seed(1)
        self.assertEqual(self._next().status_code, 200)
        self.assertEqual(self._row(jid)["status"], "processing")

    def test_concurrent_claims_no_duplicates(self):
        ids = self._seed(6)
        got, lock = [], threading.Lock()

        def worker(lane):
            from fastapi.testclient import TestClient
            client = TestClient(self.client.app)
            while True:
                r = client.get("/api/next-job", headers={"x-agent-token": "T"},
                               params={"lane": lane})
                if r.status_code != 200:
                    break
                with lock:
                    got.append(r.json()["id"])

        threads = [threading.Thread(target=worker, args=(f"lane{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(got), sorted(ids))        # каждая задача — ровно раз

    # ── caps-фильтр сохранён ─────────────────────────────────────────────────
    def test_research_not_given_without_caps(self):
        self._seed(1, mode="research")
        self.assertEqual(self._next().status_code, 204)             # старый агент
        r = self._next(caps="research")
        self.assertEqual(r.status_code, 200)                        # умеющий research

    # ── modes-фильтр дорожки: acc2 без ritual-проекта не берёт ritual ────────
    def test_modes_allowlist_limits_claims(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO jobs (input_filename, status, mode) VALUES "
                    "('r.png','pending','ritual')")
        con.execute("INSERT INTO jobs (input_filename, status, mode) VALUES "
                    "('m.png','pending','mcp')")
        con.commit()
        con.close()
        r = self.client.get("/api/next-job", headers={"x-agent-token": "T"},
                            params={"modes": "mcp,kbt"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "mcp")           # ritual пропущен
        r2 = self.client.get("/api/next-job", headers={"x-agent-token": "T"},
                             params={"modes": "mcp,kbt"})
        self.assertEqual(r2.status_code, 204)               # ritual так и не отдан

    def test_modes_empty_means_all(self):
        self._seed(1, mode="ritual")
        r = self._next()                                    # без modes — как раньше
        self.assertEqual(r.status_code, 200)

    # ── аренда (lease) ───────────────────────────────────────────────────────
    def test_stale_processing_requeued_and_reclaimed(self):
        (jid,) = self._seed(1, status="processing")
        stale = (datetime.now() - timedelta(seconds=10_000)).isoformat()
        self._set_claim(jid, "dead-lane", stale)
        r = self._next(lane="alive-lane")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], jid)                       # задачу подобрали
        self.assertEqual(self._row(jid)["claimed_by"], "alive-lane")

    def test_fresh_processing_not_requeued(self):
        (jid,) = self._seed(1, status="processing")
        fresh = (datetime.now() - timedelta(seconds=60)).isoformat()
        self._set_claim(jid, "busy-lane", fresh)
        self.assertEqual(self._next().status_code, 204)             # не выдёргиваем у живой

    def test_legacy_processing_without_claimed_at_untouched(self):
        # строки, взятые ДО апдейта (claimed_at IS NULL): над ними может прямо
        # сейчас работать агент — аренда их не трогает
        self._seed(1, status="processing")
        self.assertEqual(self._next().status_code, 204)

    # ── мягкий возврат /api/requeue (для дорожки на лимите ChatGPT) ──────────
    def test_requeue_returns_job_to_pending(self):
        (jid,) = self._seed(1, status="processing")
        self._set_claim(jid, "lane-x", datetime.now().isoformat())
        r = self.client.post(f"/api/requeue/{jid}", headers={"x-agent-token": "T"})
        self.assertEqual(r.status_code, 200)
        row = self._row(jid)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["claimed_by"])
        self.assertIsNone(row["error_text"])                        # попытка не сожжена

    def test_requeue_unknown_or_not_processing_404(self):
        self.assertEqual(self.client.post("/api/requeue/999",
                         headers={"x-agent-token": "T"}).status_code, 404)
        (jid,) = self._seed(1, status="pending")
        self.assertEqual(self.client.post(f"/api/requeue/{jid}",
                         headers={"x-agent-token": "T"}).status_code, 404)

    # ── heartbeat по дорожкам (Phase 3): общая строка + per-lane таблица ──────
    def _hb(self, lane=""):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS agent_heartbeat "
                    "(id INTEGER PRIMARY KEY, seen_at TEXT)")
        con.commit()
        con.close()
        params = {"lane": lane} if lane else {}
        return self.client.post("/api/heartbeat",
                                headers={"x-agent-token": "T"}, params=params)

    def test_heartbeat_with_lane_writes_both_tables(self):
        self.assertEqual(self._hb(lane="laptop-a1").status_code, 200)
        con = sqlite3.connect(self.db)
        # общая строка (её читает статус vps_bot — совместимость)
        self.assertIsNotNone(con.execute(
            "SELECT seen_at FROM agent_heartbeat WHERE id=1").fetchone())
        # per-lane строка — видно каждую дорожку отдельно
        row = con.execute(
            "SELECT seen_at FROM lane_heartbeat WHERE lane='laptop-a1'").fetchone()
        self.assertIsNotNone(row)

    def test_heartbeat_without_lane_legacy_only(self):
        self.assertEqual(self._hb().status_code, 200)
        con = sqlite3.connect(self.db)
        self.assertIsNotNone(con.execute(
            "SELECT seen_at FROM agent_heartbeat WHERE id=1").fetchone())
        n = con.execute("SELECT COUNT(*) FROM lane_heartbeat").fetchone()[0]
        self.assertEqual(n, 0)                       # без lane per-lane строк нет


if __name__ == "__main__":
    unittest.main()
