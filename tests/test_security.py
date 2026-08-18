"""Phase-2 security regression tests.

Covers: secret redaction (state API + logs), API auth middleware and the
non-local-bind guard, git argv execution (shell-injection fix), file-tool
path confinement, shell timeout, network scheme restriction, Telegram sender
authorization, and the telemetry memory cap.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap
from tools.secrets_tool import get_secret, redact, looks_secret, REDACTED


# ── secrets_tool ────────────────────────────────────────────────


class TestSecretsTool:
    def test_get_secret_env_resolution(self, monkeypatch):
        monkeypatch.setenv("SDLCAI_SECRET_MY_TOKEN", "s3cret")
        assert get_secret("my_token") == "s3cret"

    def test_get_secret_plain_env_fallback(self, monkeypatch):
        monkeypatch.delenv("SDLCAI_SECRET_OTHER_KEY", raising=False)
        monkeypatch.setenv("OTHER_KEY", "plain")
        assert get_secret("other_key") == "plain"

    def test_get_secret_missing(self, monkeypatch):
        monkeypatch.delenv("SDLCAI_SECRET_NOPE", raising=False)
        monkeypatch.delenv("NOPE", raising=False)
        assert get_secret("nope") is None

    def test_looks_secret(self):
        assert looks_secret("telegram_bot_token")
        assert looks_secret("LLM_API_KEY")
        assert looks_secret("password")
        assert not looks_secret("model_name")
        assert not looks_secret("timeout")

    def test_redact_nested(self):
        state = {
            "project_config": {
                "telegram_bot_token": "123:ABC",
                "telegram_chat_id": "42",
                "servers": [{"host": "h", "password": "pw"}],
            },
            "human_vision": "build things",
        }
        masked = redact(state)
        assert masked["project_config"]["telegram_bot_token"] == REDACTED
        assert masked["project_config"]["servers"][0]["password"] == REDACTED
        assert masked["project_config"]["telegram_chat_id"] == "42"
        assert masked["human_vision"] == "build things"
        # Original untouched
        assert state["project_config"]["telegram_bot_token"] == "123:ABC"


# ── Server auth & redaction ─────────────────────────────────────


@pytest.fixture()
def client():
    from starlette.testclient import TestClient
    from tools.pulse_server import app
    return TestClient(app)


class TestApiAuth:
    def test_requires_token_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("SDLCAI_API_TOKEN", "tok-123")
        assert client.get("/api/registry").status_code == 401
        assert client.get("/api/registry", headers={"X-API-Token": "wrong"}).status_code == 401
        assert client.get("/api/registry", headers={"X-API-Token": "tok-123"}).status_code == 200
        assert client.get(
            "/api/registry", headers={"Authorization": "Bearer tok-123"}
        ).status_code == 200

    def test_public_paths_stay_open(self, client, monkeypatch):
        monkeypatch.setenv("SDLCAI_API_TOKEN", "tok-123")
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200

    def test_open_when_no_token_configured(self, client, monkeypatch):
        monkeypatch.delenv("SDLCAI_API_TOKEN", raising=False)
        assert client.get("/api/registry").status_code == 200

    def test_nonlocal_bind_requires_token(self, monkeypatch):
        from tools.pulse_server import _require_token_for_nonlocal
        monkeypatch.delenv("SDLCAI_API_TOKEN", raising=False)
        _require_token_for_nonlocal("127.0.0.1")  # fine
        with pytest.raises(RuntimeError):
            _require_token_for_nonlocal("0.0.0.0")
        monkeypatch.setenv("SDLCAI_API_TOKEN", "tok")
        _require_token_for_nonlocal("0.0.0.0")  # fine with a token


class TestStateRedaction:
    def test_state_masks_credentials(self, client, monkeypatch):
        monkeypatch.delenv("SDLCAI_API_TOKEN", raising=False)
        state = bootstrap.load_project_state()
        state["project_config"] = {"telegram_bot_token": "123:ABC", "telegram_chat_id": "42"}
        bootstrap.save_project_state(state)

        data = client.get("/api/state").json()
        assert data["project_config"]["telegram_bot_token"] == REDACTED
        assert data["project_config"]["telegram_chat_id"] == "42"


class TestTelemetryCap:
    def test_agent_map_is_bounded(self, client, monkeypatch):
        import tools.pulse_server as ps
        monkeypatch.delenv("SDLCAI_API_TOKEN", raising=False)
        monkeypatch.setattr(ps, "_TELEMETRY_MAX_AGENTS", 3)
        with ps.lock:
            ps._active_agents.clear()
        for i in range(6):
            client.post("/api/telemetry", json={"agent_id": f"agent-{i}", "state": "x"})
        assert len(ps._active_agents) <= 3


# ── Git argv execution ──────────────────────────────────────────


class TestGitInjection:
    def test_commit_message_is_a_single_argument(self):
        from tools import git_tool
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        hostile = 'msg" && touch /tmp/pwned; echo "'
        with patch("tools.git_tool.subprocess.run", side_effect=fake_run):
            result = git_tool.git_commit(hostile)

        assert result["success"] is True
        commit_argv = calls[-1]
        assert commit_argv[:3] == ["git", "commit", "-m"]
        assert commit_argv[3] == hostile  # one argv element, shell-inert
        assert all(isinstance(a, str) for a in commit_argv)

    def test_hostile_commit_message_end_to_end(self, tmp_path):
        # Real git repo in the hermetic BASE_DIR: the injection payload must
        # land verbatim in the message and execute nothing.
        from tools import git_tool
        base = Path(bootstrap.BASE_DIR)
        subprocess.run(["git", "init", "-q"], cwd=base, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=base, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=base, check=True)
        (base / "f.txt").write_text("hello", encoding="utf-8")

        marker = base / "pwned"
        hostile = f'harmless" && touch {marker}; echo "'
        result = git_tool.git_commit(hostile)

        assert result["success"] is True
        assert not marker.exists()
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"], cwd=base, capture_output=True, text=True
        ).stdout
        assert "harmless" in log and "touch" in log  # stored as text, not run

    def test_branch_name_validation(self):
        from tools import git_tool
        assert git_tool.git_branch("--upload-pack=/bin/sh", "checkout")["success"] is False
        assert git_tool.git_branch("bad name; rm -rf /", "create")["success"] is False
        assert git_tool.git_checkout("$(reboot)")["success"] is False


# ── File confinement ────────────────────────────────────────────


class TestFileConfinement:
    def test_write_inside_project_allowed(self):
        from tools import file_manager
        target = Path(bootstrap.BASE_DIR) / "notes.txt"
        assert file_manager.write_file(str(target), "ok")["success"] is True
        assert file_manager.read_file(str(target))["content"] == "ok"

    def test_absolute_escape_denied(self):
        from tools import file_manager
        result = file_manager.read_file("/etc/hostname")
        assert result["success"] is False
        assert "outside the allowed" in result["error"]

    def test_traversal_escape_denied(self):
        from tools import file_manager
        sneaky = str(Path(bootstrap.BASE_DIR) / ".." / ".." / "etc" / "passwd")
        result = file_manager.read_file(sneaky)
        assert result["success"] is False

    def test_write_escape_denied(self, tmp_path_factory):
        from tools import file_manager
        outside = tmp_path_factory.mktemp("outside") / "evil.txt"
        result = file_manager.write_file(str(outside), "x")
        assert result["success"] is False
        assert not outside.exists()

    def test_delete_escape_denied(self, tmp_path_factory):
        from tools import file_manager
        outside = tmp_path_factory.mktemp("outside2") / "keep.txt"
        outside.write_text("precious", encoding="utf-8")
        result = file_manager.delete_file(str(outside))
        assert result["success"] is False
        assert outside.exists()

    def test_extra_roots_via_env(self, tmp_path_factory, monkeypatch):
        from tools import file_manager
        extra = tmp_path_factory.mktemp("extra_root")
        monkeypatch.setenv("SDLCAI_FILE_ROOTS", str(extra))
        assert file_manager.write_file(str(extra / "ok.txt"), "y")["success"] is True


# ── Shell timeout & network schemes ─────────────────────────────


class TestShellTimeout:
    def test_command_times_out(self):
        from tools.shell_executor import execute_command
        result = execute_command("sleep 5", timeout=1)
        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"]

    def test_normal_command_still_works(self):
        from tools.shell_executor import execute_command
        result = execute_command("echo hi")
        assert result["exit_code"] == 0
        assert result["stdout"] == "hi"


class TestNetworkSchemes:
    def test_file_scheme_denied(self):
        from tools import network_tool
        result = network_tool.get("file:///etc/passwd")
        assert result["success"] is False
        assert "not allowed" in result["error"]

    def test_post_scheme_denied(self):
        from tools import network_tool
        result = network_tool.post("gopher://x", json_data={})
        assert result["success"] is False


# ── Telegram sender authorization ───────────────────────────────


def _tg_update(chat_id="42", chat_type="private", sender_id="42", text="my answer"):
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": sender_id},
            "text": text,
            "reply_to_message": {"message_id": 99},
        },
    }


class TestTelegramAuthorization:
    def _process(self, update, monkeypatch, allowlist=""):
        from tools import telegram_bot, clarification_tool
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", allowlist)
        answered = MagicMock(return_value={"id": "CLR-0001"})
        with patch.object(telegram_bot, "get_config",
                          return_value={"token": "t", "chat_id": "42"}), \
             patch.object(telegram_bot, "send_message"), \
             patch.object(clarification_tool, "get_request_by_telegram_message",
                          return_value={"id": "CLR-0001"}), \
             patch.object(clarification_tool, "answer_request", answered):
            poller = telegram_bot.TelegramBotPoller()
            poller._process_update(update)
        return answered

    def test_private_configured_chat_accepted(self, monkeypatch):
        answered = self._process(_tg_update(), monkeypatch)
        answered.assert_called_once()

    def test_group_chat_rejected_without_allowlist(self, monkeypatch):
        answered = self._process(_tg_update(chat_type="group"), monkeypatch)
        answered.assert_not_called()

    def test_wrong_chat_rejected(self, monkeypatch):
        answered = self._process(_tg_update(chat_id="666"), monkeypatch)
        answered.assert_not_called()

    def test_allowlist_enforced(self, monkeypatch):
        answered = self._process(
            _tg_update(sender_id="777"), monkeypatch, allowlist="111,222"
        )
        answered.assert_not_called()

    def test_allowlisted_sender_accepted_from_any_chat(self, monkeypatch):
        answered = self._process(
            _tg_update(chat_type="group", sender_id="111"), monkeypatch, allowlist="111"
        )
        answered.assert_called_once()


# ── Log redaction ───────────────────────────────────────────────


class TestAgentLoggerRedaction:
    def test_secret_env_values_masked(self, monkeypatch):
        from tools.agent_logger import _collect_env_vars
        monkeypatch.setenv("API_KEY", "supersecret")
        monkeypatch.setenv("LLM_API_KEY", "alsosecret")
        monkeypatch.setenv("MODEL_NAME", "qwen")
        env = _collect_env_vars()
        assert env["API_KEY"] == REDACTED
        assert env["LLM_API_KEY"] == REDACTED
        assert env["MODEL_NAME"] == "qwen"
        assert "supersecret" not in json.dumps(env)