"""Дорожки (lanes) — мульти-аккаунт ChatGPT (Phase 3 спеки 2026-07-06).
lanes.json: карта машин (MachineGuid → имя) + список дорожек с ПРИВЯЗКОЙ к машине.
my_lanes() отдаёт только дорожки СВОЕЙ машины — иначе оба вотчдога подняли бы
все дорожки и задвоили аккаунты (ревью 2026-07-06). project_url на дорожку —
литералом или 'env:ИМЯ' (сам URL остаётся в .env, lanes.json — в git)."""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import Lane, get_lane, load_lanes, machine_id, my_lanes  # noqa: E402


def _write(tmp_path, machines=None, lanes=None):
    p = tmp_path / "lanes.json"
    p.write_text(json.dumps({"machines": machines or {}, "lanes": lanes or []},
                            ensure_ascii=False), encoding="utf-8")
    return p


LANES = [
    {"id": "laptop-a1", "machine": "laptop", "cdp_port": 9333,
     "profile_dir": "chrome_profile", "enabled": True},
    {"id": "laptop-a2", "machine": "laptop", "cdp_port": 9334,
     "profile_dir": "chrome_profile_acc2", "enabled": False,
     "project_urls": {"mcp": "env:MCP_PROJECT_URL_ACC2",
                      "kbt": "https://chatgpt.com/g/literal"}},
    {"id": "desktop-a1", "machine": "desktop", "cdp_port": 9333,
     "profile_dir": "chrome_profile", "enabled": True},
]


def test_load_lanes_parses_all(tmp_path):
    p = _write(tmp_path, lanes=LANES)
    lanes = load_lanes(p)
    assert [l.id for l in lanes] == ["laptop-a1", "laptop-a2", "desktop-a1"]
    assert lanes[0].cdp_url == "http://127.0.0.1:9333"
    assert lanes[1].cdp_url == "http://127.0.0.1:9334"


def test_load_lanes_missing_file_empty(tmp_path):
    assert load_lanes(tmp_path / "nope.json") == []


def test_machine_id_env_override(monkeypatch):
    monkeypatch.setenv("LANE_MACHINE_GUID", "guid-test")
    assert machine_id() == "guid-test"


def test_my_lanes_filters_by_machine_and_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LANE_MACHINE_GUID", "guid-lap")
    p = _write(tmp_path, machines={"guid-lap": "laptop", "guid-desk": "desktop"},
               lanes=LANES)
    got = my_lanes(p)
    assert [l.id for l in got] == ["laptop-a1"]      # чужая машина и disabled отсечены


def test_my_lanes_unknown_machine_empty(tmp_path, monkeypatch):
    # GUID не в карте → НИ ОДНОЙ дорожки (безопасный дефолт: не поднимать чужое)
    monkeypatch.setenv("LANE_MACHINE_GUID", "guid-alien")
    p = _write(tmp_path, machines={"guid-lap": "laptop"}, lanes=LANES)
    assert my_lanes(p) == []


def test_get_lane_by_id(tmp_path):
    p = _write(tmp_path, lanes=LANES)
    assert get_lane("laptop-a2", p).cdp_port == 9334
    assert get_lane("nope", p) is None


def test_project_url_literal_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_PROJECT_URL_ACC2", "https://chatgpt.com/g/from-env")
    lane = get_lane("laptop-a2", _write(tmp_path, lanes=LANES))
    assert lane.project_url_for("mcp") == "https://chatgpt.com/g/from-env"
    assert lane.project_url_for("kbt") == "https://chatgpt.com/g/literal"
    assert lane.project_url_for("ritual") == ""      # нет override → фолбэк у вызывающего


def test_project_url_env_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_PROJECT_URL_ACC2", raising=False)
    lane = get_lane("laptop-a2", _write(tmp_path, lanes=LANES))
    assert lane.project_url_for("mcp") == ""
