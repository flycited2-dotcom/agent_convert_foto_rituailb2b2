"""Вотчдог на N дорожек (Phase 5): per-lane state-файлы, паттерны поиска
процессов, выбор целей (дорожки машины | legacy-одиночка). Чистые хелперы —
без запуска powershell/Chrome."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import Lane  # noqa: E402
import agent_watchdog as wd  # noqa: E402

LANE_A = Lane(id="laptop-a1", machine="laptop", cdp_port=9333,
              profile_dir="chrome_profile")
LANE_B = Lane(id="laptop-a2", machine="laptop", cdp_port=9334,
              profile_dir="chrome_profile_acc2")


def test_state_files_are_per_lane():
    legacy = wd.state_file(None)
    a = wd.state_file(LANE_A)
    b = wd.state_file(LANE_B)
    assert legacy.name == "agent_state.txt"          # без дорожки — как раньше
    assert a.name == "agent_state_laptop-a1.txt"
    assert b.name == "agent_state_laptop-a2.txt"
    assert len({legacy, a, b}) == 3                  # не пересекаются


def test_desired_state_roundtrip_per_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "_LOGS_DIR", tmp_path)
    wd.set_desired_state("running", LANE_A)
    wd.set_desired_state("stopped", LANE_B)
    assert wd.desired_state(LANE_A) == "running"
    assert wd.desired_state(LANE_B) == "stopped"
    assert wd.desired_state(None) == "stopped"       # legacy-файл не тронут → дефолт


def test_agent_match_pattern_scoped_by_lane():
    assert wd.agent_match_pattern(None) == "remote_agent"
    pat = wd.agent_match_pattern(LANE_A)
    assert "remote_agent" in pat and "laptop-a1" in pat


def test_cdp_url_per_lane():
    assert wd.cdp_url_for(LANE_B) == "http://127.0.0.1:9334"
    assert wd.cdp_url_for(None) == wd.CHROME_CDP_URL   # legacy


def test_targets_lanes_or_legacy_single(monkeypatch):
    monkeypatch.setattr(wd, "LANES", [LANE_A, LANE_B])
    assert wd.targets() == [LANE_A, LANE_B]
    monkeypatch.setattr(wd, "LANES", [])
    assert wd.targets() == [None]                    # legacy: одна безымянная дорожка
