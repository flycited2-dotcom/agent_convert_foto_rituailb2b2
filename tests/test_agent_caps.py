"""caps агента должны зависеть от настроенности режимов (2026-07-07):
десктоп без RESEARCH_PROJECT_URL слал caps=research безусловно → брал
research-задачи и жёг их «режим не настроен» (job 731)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class _FakeMode:
    def __init__(self, url="", prompt="p", enabled=True):
        self.project_url = url
        self.prompt = prompt
        self.enabled = enabled


class _FakeLane:
    def __init__(self, research_url=""):
        self._u = research_url

    def project_url_for(self, mode_key):
        return self._u if mode_key == "research" else ""


def test_agent_caps_reflect_research_configured(monkeypatch):
    import remote_agent

    monkeypatch.setattr(remote_agent, "get_mode",
                        lambda key: _FakeMode(url="https://x/project"))
    assert remote_agent.agent_caps() == "research"

    monkeypatch.setattr(remote_agent, "get_mode", lambda key: _FakeMode(url=""))
    assert remote_agent.agent_caps() == ""       # не настроен → не заявляем


def test_agent_caps_respect_lane_override(monkeypatch):
    # дедлок 2026-07-07: у desktop-a2 research настроен ЧЕРЕЗ override дорожки
    # (RESEARCH_PROJECT_URL_ACC2), а caps смотрел только модульный env →
    # research-задачи не брал никто (ноут в standby по аренде)
    import remote_agent

    monkeypatch.setattr(remote_agent, "get_mode", lambda key: _FakeMode(url=""))
    monkeypatch.setattr(remote_agent, "LANE", _FakeLane("https://acc2/project"))
    assert remote_agent.agent_caps() == "research"

    monkeypatch.setattr(remote_agent, "LANE", _FakeLane(""))
    assert remote_agent.agent_caps() == ""

    monkeypatch.setattr(remote_agent, "LANE", None)   # без дорожки — как раньше
    assert remote_agent.agent_caps() == ""
