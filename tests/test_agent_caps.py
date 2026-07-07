"""caps агента должны зависеть от настроенности режимов (2026-07-07):
десктоп без RESEARCH_PROJECT_URL слал caps=research безусловно → брал
research-задачи и жёг их «режим не настроен» (job 731)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_agent_caps_reflect_research_configured(monkeypatch):
    import remote_agent
    import config

    class _FakeMode:
        def __init__(self, ok):
            self.is_configured = ok

    monkeypatch.setattr(remote_agent, "get_mode",
                        lambda key: _FakeMode(True))
    assert remote_agent.agent_caps() == "research"

    monkeypatch.setattr(remote_agent, "get_mode",
                        lambda key: _FakeMode(False))
    assert remote_agent.agent_caps() == ""       # не настроен → не заявляем
