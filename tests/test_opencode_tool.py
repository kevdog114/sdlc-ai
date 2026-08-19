"""Tests for tools/opencode_tool.py — the HTTP client + real server lifecycle.

Rewritten from the old drifted suite: opencode_tool is now a thin client with
real port discovery, an honest is_installed()/start_server() (no stubs), a
model resolver, and un-shadowed event logging.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import opencode_tool as oc


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    return r


class TestPortDiscovery:
    def setup_method(self):
        oc._active_port = None

    def test_discovers_and_caches_port(self):
        # Health OK only on the second scanned port (4097).
        def fake_get(url, timeout=None):
            port = int(url.split(":")[2].split("/")[0])
            return _resp(200 if port == 4097 else 500)
        with patch("tools.opencode_tool.requests.get", side_effect=fake_get):
            assert oc._discover_port() == 4097
        assert oc._active_port == 4097

    def test_is_server_running_false_when_none(self):
        with patch("tools.opencode_tool.requests.get", side_effect=oc.requests.RequestException):
            assert oc.is_server_running() is False

    def test_base_url_uses_discovered_port(self):
        oc._active_port = 4099
        with patch("tools.opencode_tool.requests.get", return_value=_resp(200)):
            assert oc._get_base_url() == "http://127.0.0.1:4099"


class TestIsInstalled:
    def test_true_when_binary_on_path(self):
        with patch("tools.opencode_tool.shutil.which", return_value="/usr/bin/opencode"):
            assert oc.is_installed() is True

    def test_false_when_missing(self):
        with patch("tools.opencode_tool.shutil.which", return_value=None):
            assert oc.is_installed() is False


class TestStartServer:
    def setup_method(self):
        oc._active_port = None
        oc._server_process = None

    def test_reuses_running_server(self):
        with patch("tools.opencode_tool._discover_port", return_value=4096):
            result = oc.start_server()
        assert result["success"] is True
        assert result["reused"] is True

    def test_fails_loudly_when_not_installed(self):
        # The core regression: the old stub returned success unconditionally.
        with patch("tools.opencode_tool._discover_port", return_value=None), \
             patch("tools.opencode_tool.is_installed", return_value=False):
            result = oc.start_server()
        assert result["success"] is False
        assert "not found on PATH" in result["error"]

    def test_spawns_and_waits_for_health(self):
        proc = MagicMock()
        proc.poll.return_value = None
        with patch("tools.opencode_tool._discover_port", return_value=None), \
             patch("tools.opencode_tool.is_installed", return_value=True), \
             patch("tools.opencode_tool.subprocess.Popen", return_value=proc), \
             patch("tools.opencode_tool._health_ok", return_value=True):
            result = oc.start_server(port=4096)
        assert result["success"] is True
        assert oc._active_port == 4096

    def test_reports_startup_crash(self):
        proc = MagicMock()
        proc.poll.return_value = 1  # exited immediately
        proc.returncode = 1
        with patch("tools.opencode_tool._discover_port", return_value=None), \
             patch("tools.opencode_tool.is_installed", return_value=True), \
             patch("tools.opencode_tool.subprocess.Popen", return_value=proc), \
             patch("tools.opencode_tool._health_ok", return_value=False):
            result = oc.start_server(port=4096)
        assert result["success"] is False
        assert "exited during startup" in result["error"]


class TestStopServer:
    def test_stops_running_process(self):
        proc = MagicMock()
        proc.poll.return_value = None
        oc._server_process = proc
        result = oc.stop_server()
        assert result["success"] is True
        proc.terminate.assert_called_once()
        assert oc._server_process is None

    def test_noop_when_not_started(self):
        oc._server_process = None
        result = oc.stop_server()
        assert result["success"] is True
        assert result["already_stopped"] is True


class TestModelResolution:
    def test_provider_slash_model(self):
        assert oc._resolve_model("anthropic/claude-x") == ("anthropic", "claude-x")

    def test_bare_model_uses_default_provider(self):
        provider, model = oc._resolve_model("some-model")
        assert provider == oc._DEFAULT_PROVIDER
        assert model == "some-model"

    def test_none_uses_defaults(self):
        assert oc._resolve_model(None) == (oc._DEFAULT_PROVIDER, oc._DEFAULT_MODEL_ID)


class TestExecuteTask:
    def setup_method(self):
        oc._active_port = 4096

    def test_returns_error_when_server_down(self):
        with patch("tools.opencode_tool.is_server_running", return_value=False):
            result = oc.execute_task(task_description="do it", system_prompt="be good")
        assert result["success"] is False
        assert "not running" in result["error"]

    def test_model_parameter_is_honored(self):
        posts = []

        def fake_post(endpoint, json_body, **kwargs):
            posts.append((endpoint, json_body))
            if endpoint == "/session":
                return {"id": "sess-1"}
            return {"parts": [{"type": "text", "text": "done"}]}

        with patch("tools.opencode_tool.is_server_running", return_value=True), \
             patch("tools.opencode_tool._api_post", side_effect=fake_post), \
             patch("tools.opencode_tool._get_session_diffs", return_value=[]):
            oc.execute_task(
                task_description="do it",
                system_prompt="be good",
                model="anthropic/claude-x",
            )
        message_post = next(b for e, b in posts if e.endswith("/message"))
        assert message_post["model"] == {"providerID": "anthropic", "modelID": "claude-x"}


class TestEventLoggingNotShadowed:
    def test_append_event_is_the_real_one(self):
        # Regression: opencode_tool used to define a print()-only append_event,
        # so its activity never reached the event log.
        import bootstrap
        assert oc.append_event is bootstrap.append_event


class TestExtractOutput:
    def test_from_parts(self):
        msg = {"parts": [{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}]}
        assert "hello" in oc._extract_output(msg)

    def test_empty_no_parts(self):
        assert oc._extract_output({}) == ""
