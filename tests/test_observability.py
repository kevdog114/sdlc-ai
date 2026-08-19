"""Phase-3 tests: the event stream and telemetry wiring the dashboard needs."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap


@pytest.fixture()
def client():
    from starlette.testclient import TestClient
    from tools.pulse_server import app
    return TestClient(app)


class TestEventStream:
    def test_events_endpoint_returns_log_tail(self, client, monkeypatch):
        monkeypatch.delenv("SDLCAI_API_TOKEN", raising=False)
        bootstrap.append_event("test:one", {"n": 1})
        bootstrap.append_event("test:two", {"n": 2})

        data = client.get("/api/events").json()
        assert data["count"] >= 2
        types = [e.get("type") for e in data["events"]]
        assert "test:one" in types and "test:two" in types

    def test_events_endpoint_respects_limit(self, client, monkeypatch):
        monkeypatch.delenv("SDLCAI_API_TOKEN", raising=False)
        for i in range(5):
            bootstrap.append_event("test:evt", {"i": i})
        data = client.get("/api/events", params={"limit": 3}).json()
        assert len(data["events"]) == 3

    def test_safe_read_events_is_not_stubbed(self, monkeypatch):
        # Regression: _safe_read_events used to hardcode [] (stream disabled).
        import tools.pulse_server as ps
        bootstrap.append_event("test:read", {"ok": True})
        lines = ps._safe_read_events()
        assert any('"test:read"' in ln for ln in lines)


class TestTelemetryDefaults:
    def test_agent_manager_default_targets_server_port(self, monkeypatch):
        # The port mismatch bug: agents defaulted to 8081, server is 8080.
        monkeypatch.delenv("PULSE_SERVER_URL", raising=False)
        import tools.agent_manager as am
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            import types
            proc = types.SimpleNamespace(pid=4242, poll=lambda: None)
            return proc

        monkeypatch.setattr(am.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(am, "get_task_by_id", lambda tid: None)
        am.spawn_agent(goal="do", persona="researcher")

        cmd = captured["command"]
        assert "--pulse_url" in cmd
        url = cmd[cmd.index("--pulse_url") + 1]
        assert "8080" in url and "8081" not in url

    def test_pulse_url_env_override(self, monkeypatch):
        monkeypatch.setenv("PULSE_SERVER_URL", "http://example:9999")
        import tools.agent_manager as am
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            import types
            return types.SimpleNamespace(pid=1, poll=lambda: None)

        monkeypatch.setattr(am.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(am, "get_task_by_id", lambda tid: None)
        am.spawn_agent(goal="do", persona="researcher")

        cmd = captured["command"]
        assert cmd[cmd.index("--pulse_url") + 1] == "http://example:9999"
