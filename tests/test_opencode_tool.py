"""Tests for opencode_tool - OpenCode server management and task execution."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))


class TestIsInstalled:
    def test_returns_true_when_available(self):
        with patch('tools.opencode_tool.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            from tools.opencode_tool import is_installed
            assert is_installed() is True

    def test_returns_false_when_missing(self):
        with patch('tools.opencode_tool.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            from tools.opencode_tool import is_installed
            assert is_installed() is False


class TestServerLifecycle:
    def test_start_server_not_installed(self):
        with patch('tools.opencode_tool.is_server_running', return_value=False):
            with patch('tools.opencode_tool.is_installed', return_value=False):
                with patch('tools.opencode_tool.append_event'):
                    from tools.opencode_tool import start_server
                    result = start_server()
                    assert result["success"] is False
                    assert "not installed" in result["error"]

    def test_start_server_already_running(self):
        import tools.opencode_tool as oc
        orig_proc = oc._server_process
        orig_port = oc._server_port
        try:
            oc._server_process = MagicMock()
            oc._server_process.poll.return_value = None
            oc._server_port = 4096
            result = oc.start_server()
            assert result["success"] is True
            assert result["already_running"] is True
        finally:
            oc._server_process = orig_proc
            oc._server_port = orig_port

    def test_start_server_reuses_existing_network_server(self):
        import tools.opencode_tool as oc
        orig_proc = oc._server_process
        orig_port = oc._server_port
        try:
            oc._server_process = None
            oc._server_port = None
            with patch('tools.opencode_tool.is_server_running', return_value=True):
                with patch('tools.opencode_tool._get_base_url', return_value='http://127.0.0.1:4096'):
                    result = oc.start_server()
                    assert result["success"] is True
                    assert result["already_running"] is True
                    assert result["reused"] is True
        finally:
            oc._server_process = orig_proc
            oc._server_port = orig_port

    def test_stop_server_already_stopped(self):
        import tools.opencode_tool as oc
        orig_proc = oc._server_process
        try:
            oc._server_process = None
            result = oc.stop_server()
            assert result["success"] is True
            assert result["already_stopped"] is True
        finally:
            oc._server_process = orig_proc

    def test_stop_server_running(self):
        import tools.opencode_tool as oc
        orig_proc = oc._server_process
        orig_port = oc._server_port
        try:
            mock_proc = MagicMock()
            oc._server_process = mock_proc
            oc._server_port = 4096
            with patch('tools.opencode_tool.append_event'):
                result = oc.stop_server()
            assert result["success"] is True
            mock_proc.send_signal.assert_called_once()
            assert oc._server_process is None
            assert oc._server_port is None
        finally:
            oc._server_process = orig_proc
            oc._server_port = orig_port

    def test_is_server_running_true(self):
        import tools.opencode_tool as oc
        orig_port = oc._server_port
        try:
            oc._server_port = 4096
            with patch('tools.opencode_tool.requests.get') as mock_get:
                mock_get.return_value = MagicMock(status_code=200)
                assert oc.is_server_running() is True
        finally:
            oc._server_port = orig_port

    def test_is_server_running_false_no_port(self):
        import tools.opencode_tool as oc
        orig_port = oc._server_port
        try:
            oc._server_port = None
            with patch('tools.opencode_tool.socket.socket') as mock_socket:
                mock_socket.return_value.__enter__.return_value.connect_ex.return_value = 1
                assert oc.is_server_running() is False
        finally:
            oc._server_port = orig_port


class TestExecuteTask:
    def test_returns_error_server_not_running(self):
        with patch('tools.opencode_tool.is_server_running', return_value=False):
            with patch('tools.opencode_tool.append_event'):
                from tools.opencode_tool import execute_task
                result = execute_task("test", "system")
                assert result["success"] is False
                assert "not running" in result["error"]

    def test_successful_task_execution(self):
        import tools.opencode_tool as oc
        orig_port = oc._server_port
        try:
            oc._server_port = 4096

            def fake_post(path, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if path.endswith("/session"):
                    resp.json.return_value = {"id": "test-session"}
                elif "/message" in path:
                    resp.json.return_value = {
                        "info": {"id": "msg-1"},
                        "parts": [{"content": "Implementation complete."}]
                    }
                else:
                    resp.json.return_value = {}
                return resp

            def fake_get(path, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if "/diff" in path:
                    resp.json.return_value = [{"path": "src/module.py", "diff": "+new code"}]
                elif "/health" in path:
                    resp.json.return_value = {"healthy": True}
                else:
                    resp.json.return_value = {}
                return resp

            with patch('tools.opencode_tool.requests.post', side_effect=fake_post):
                with patch('tools.opencode_tool.requests.get', side_effect=fake_get):
                    with patch('tools.opencode_tool.append_event'):
                        result = oc.execute_task(
                            task_description="Implement feature",
                            system_prompt="You are a developer.",
                            interface_spec="GET /api/feature",
                            additional_context="Use existing patterns.",
                        )

            assert result["success"] is True
            assert result["session_id"] == "test-session"
            assert "Implementation complete" in result["output"]
            assert len(result["diffs"]) == 1
            assert result["diffs"][0]["path"] == "src/module.py"
        finally:
            oc._server_port = orig_port

    def test_interface_spec_included_in_message(self):
        import tools.opencode_tool as oc
        orig_port = oc._server_port
        try:
            oc._server_port = 4096
            captured_body = {}

            def fake_post(path, **kwargs):
                nonlocal captured_body
                resp = MagicMock()
                resp.status_code = 200
                if path.endswith("/session"):
                    resp.json.return_value = {"id": "sess-1"}
                elif "/message" in path:
                    captured_body = kwargs.get("json") or {}
                    resp.json.return_value = {
                        "info": {"id": "msg-1"},
                        "parts": [{"content": "Done."}]
                    }
                else:
                    resp.json.return_value = {}
                return resp

            def fake_get(path, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if "/health" in path:
                    resp.json.return_value = {"healthy": True}
                else:
                    resp.json.return_value = []
                return resp

            with patch('tools.opencode_tool.requests.post', side_effect=fake_post):
                with patch('tools.opencode_tool.requests.get', side_effect=fake_get):
                    with patch('tools.opencode_tool.append_event'):
                        oc.execute_task(
                            task_description="Build API",
                            system_prompt="Developer.",
                            interface_spec="GET /users",
                        )

            parts = captured_body.get("parts", [])
            msg = parts[0].get("text", "") if parts else ""
            assert "INTERFACE CONTRACT" in msg
            assert "GET /users" in msg
        finally:
            oc._server_port = orig_port


class TestExtractOutput:
    def test_from_parts(self):
        from tools.opencode_tool import _extract_output
        resp = {"parts": [{"content": "Hello"}, {"content": " World"}]}
        assert _extract_output(resp) == "Hello\n World"

    def test_empty_no_parts(self):
        from tools.opencode_tool import _extract_output
        assert _extract_output({}) == ""

    def test_fallback_text_key(self):
        from tools.opencode_tool import _extract_output
        assert _extract_output({"parts": [{"text": "fallback"}]}) == "fallback"
