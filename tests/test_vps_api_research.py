"""Тесты research-эндпоинтов vps_api (submit-research / complete-research).
Без сети: fastapi TestClient + временная queue.db (schema как init_db vps_bot)."""
import io
import importlib
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vps"))


class ResearchApiTest(unittest.TestCase):
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
        config_vps.DB_PATH = root / "queue.db"   # в config_vps путь хардкод — патчим
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
        self.out_dir = Path(os.environ["OUTPUT_DIR"])

    def tearDown(self):
        self.tmp.cleanup()

    def _submit(self, **over):
        data = {"brand": "Beko", "model": "B1RCSK362S",
                "category": "холодильник", "chat_id": "42"}
        data.update(over)
        r = self.client.post("/api/submit-research",
                             headers={"x-agent-token": "T"}, data=data)
        self.assertEqual(r.status_code, 200)
        return r.json()["job_id"]

    def test_submit_research_creates_pending_job(self):
        job_id = self._submit()
        row = sqlite3.connect(self.db).execute(
            "SELECT mode, brand, model, specs, status, input_filename FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        self.assertEqual(row, ("research", "Beko", "B1RCSK362S",
                               "холодильник", "pending", ""))

    def test_complete_research_saves_photo_and_utp(self):
        job_id = self._submit()
        r = self.client.post(f"/api/complete-research/{job_id}",
                             headers={"x-agent-token": "T"},
                             data={"utp": "✓ No Frost\n✓ Тихий 39 дБ"},
                             files={"photo": ("beko.png", io.BytesIO(b"PNG"), "image/png")})
        self.assertEqual(r.status_code, 200)
        row = sqlite3.connect(self.db).execute(
            "SELECT status, result_specs, output_filename, result_sent FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        self.assertEqual(row[0], "done")
        self.assertIn("No Frost", row[1])
        self.assertTrue(row[2])
        self.assertEqual(row[3], 1)                       # result_sender не рассылает
        self.assertTrue((self.out_dir / row[2]).exists()) # фото легло в OUTPUT_DIR

    def test_complete_research_without_photo(self):
        job_id = self._submit(model="M")
        r = self.client.post(f"/api/complete-research/{job_id}",
                             headers={"x-agent-token": "T"}, data={"utp": "✓ x"})
        self.assertEqual(r.status_code, 200)
        row = sqlite3.connect(self.db).execute(
            "SELECT status, output_filename FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row, ("done", None))   # фото нет → карточка потом «по названию»

    def test_next_job_hides_research_from_legacy_agents(self):
        """Старый агент (без caps) research не получает — иначе фейлит «битой задачей»."""
        self._submit()
        r = self.client.get("/api/next-job", headers={"x-agent-token": "T"})
        self.assertEqual(r.status_code, 204)      # research скрыт, других задач нет

    def test_next_job_gives_research_to_capable_agents(self):
        job_id = self._submit()
        r = self.client.get("/api/next-job", headers={"x-agent-token": "T"},
                            params={"caps": "research"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], job_id)
        self.assertEqual(r.json()["mode"], "research")

    def test_agent_command_worker_scoped_flags(self):
        """Адресные флаги: ноут забирает только свой ключ, десктоп (без worker) — общий."""
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO flags VALUES ('agent_command', 'stop')")
        con.execute("INSERT INTO flags VALUES ('agent_command_laptop', 'start')")
        con.commit()
        con.close()
        r1 = self.client.get("/api/agent-command", headers={"x-agent-token": "T"},
                             params={"worker": "laptop"})
        self.assertEqual(r1.json()["command"], "start")     # ноут взял свой
        r2 = self.client.get("/api/agent-command", headers={"x-agent-token": "T"})
        self.assertEqual(r2.json()["command"], "stop")      # старый десктоп — общий
        r3 = self.client.get("/api/agent-command", headers={"x-agent-token": "T"},
                             params={"worker": "laptop"})
        self.assertEqual(r3.json()["command"], "none")      # one-shot: повторно пусто

    def test_bad_token_rejected(self):
        r = self.client.post("/api/submit-research", headers={"x-agent-token": "BAD"},
                             data={"brand": "B", "model": "M", "chat_id": "1"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
