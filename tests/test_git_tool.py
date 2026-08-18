"""Tests for tools/git_tool.py — argv-based git execution.

git_tool runs argv lists (never shell=True) with cwd=bootstrap.BASE_DIR, so
LLM-authored commit messages and branch names are inert arguments. Unit tests
patch subprocess.run; integration tests run real git inside the hermetic
BASE_DIR provided by conftest.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap
from tools.git_tool import (
    git_status,
    git_commit,
    git_branch,
    git_checkout,
    git_pull,
    git_push,
    git_diff,
    git_current_branch,
)


def _mock_run(returncode=0, stdout="success output", stderr=""):
    def fake(argv, **kwargs):
        fake.calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    fake.calls = []
    return fake


class TestGitStatus:
    def test_status_success(self):
        run = _mock_run(stdout="## main\n M tools/git_tool.py\n")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_status()
        assert result["success"] is True
        assert result["error"] is None
        assert "## main" in result["output"]
        assert run.calls[0][:2] == ["git", "status"]

    def test_status_failure(self):
        run = _mock_run(returncode=1, stdout="", stderr="not a git repository")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_status()
        assert result["success"] is False
        assert result["error"] == "not a git repository"
        assert result["output"] == ""

    def test_append_event_called(self):
        run = _mock_run(stdout="")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event') as mock_event:
                git_status()
        mock_event.assert_called_once()
        assert mock_event.call_args[0][0] == "tool:git_operation"
        assert mock_event.call_args[0][1]["operation"] == "status"
        assert mock_event.call_args[0][1]["success"] is True

    def test_runs_in_project_root(self):
        run = _mock_run(stdout="")
        captured = {}

        def fake(argv, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch('tools.git_tool.subprocess.run', side_effect=fake):
            with patch('tools.git_tool.append_event'):
                git_status()
        assert captured["cwd"] == str(bootstrap.BASE_DIR)


class TestGitCommit:
    def test_commit_success(self):
        run = _mock_run(stdout="[main abc123] Test commit\n")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_commit("Test commit")
        assert result["success"] is True
        # Two argv invocations: add, then commit with the message as ONE arg.
        assert run.calls[0] == ["git", "add", "-A"]
        assert run.calls[1] == ["git", "commit", "-m", "Test commit"]

    def test_commit_message_never_interpolated(self):
        run = _mock_run()
        hostile = 'msg" && rm -rf / #'
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                git_commit(hostile)
        assert run.calls[1][3] == hostile  # verbatim single argument

    def test_commit_empty_message(self):
        with patch('tools.git_tool.subprocess.run') as mock_run:
            with patch('tools.git_tool.append_event'):
                result = git_commit("")
        assert result["success"] is False
        assert "empty" in result["error"].lower()
        mock_run.assert_not_called()

    def test_commit_whitespace_only_message(self):
        with patch('tools.git_tool.subprocess.run') as mock_run:
            with patch('tools.git_tool.append_event'):
                result = git_commit("   ")
        assert result["success"] is False
        mock_run.assert_not_called()

    def test_commit_failure(self):
        run = _mock_run(returncode=1, stdout="", stderr="nothing to commit")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_commit("Test commit")
        assert result["success"] is False
        assert result["error"] == "nothing to commit"

    def test_append_event_called(self):
        run = _mock_run(stdout="[main abc123] Test\n")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event') as mock_event:
                git_commit("Test commit")
        # add + commit each log an event; the last is the commit itself.
        assert mock_event.call_args[0][1]["operation"] == "commit"


class TestGitBranch:
    def test_branch_create_success(self):
        run = _mock_run(stdout="")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_branch("feature-test")
        assert result["success"] is True
        assert run.calls[0] == ["git", "branch", "feature-test"]

    def test_branch_checkout_action(self):
        run = _mock_run(stdout="Switched to branch 'feature-test'")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_branch("feature-test", action="checkout")
        assert result["success"] is True
        assert run.calls[0] == ["git", "checkout", "feature-test"]

    def test_branch_delete_action(self):
        run = _mock_run(stdout="Deleted branch feature-test")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_branch("feature-test", action="delete")
        assert result["success"] is True
        assert run.calls[0] == ["git", "branch", "-d", "feature-test"]

    def test_branch_list_action(self):
        run = _mock_run(stdout="  main\n* feature-test")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_branch(action="list")
        assert result["success"] is True
        assert run.calls[0] == ["git", "branch"]

    def test_branch_unknown_action(self):
        with patch('tools.git_tool.subprocess.run') as mock_run:
            with patch('tools.git_tool.append_event'):
                result = git_branch("test", action="rename")
        assert result["success"] is False
        assert "Unknown branch action" in result["error"]
        mock_run.assert_not_called()

    def test_branch_invalid_name_rejected(self):
        with patch('tools.git_tool.subprocess.run') as mock_run:
            with patch('tools.git_tool.append_event'):
                result = git_branch("bad name; rm -rf /", action="create")
        assert result["success"] is False
        assert "Invalid branch name" in result["error"]
        mock_run.assert_not_called()

    def test_branch_option_like_name_rejected(self):
        with patch('tools.git_tool.subprocess.run') as mock_run:
            with patch('tools.git_tool.append_event'):
                result = git_branch("--upload-pack=/bin/sh", action="checkout")
        assert result["success"] is False
        mock_run.assert_not_called()

    def test_branch_create_failure(self):
        run = _mock_run(returncode=1, stdout="", stderr="branch already exists")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_branch("existing-branch")
        assert result["success"] is False
        assert result["error"] == "branch already exists"


class TestGitCheckout:
    def test_checkout_success(self):
        run = _mock_run(stdout="Switched to branch 'main'")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_checkout("main")
        assert result["success"] is True
        assert run.calls[0] == ["git", "checkout", "main"]

    def test_checkout_nonexistent_branch(self):
        run = _mock_run(returncode=1, stdout="", stderr="error: pathspec 'nonexistent' did not match")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_checkout("nonexistent")
        assert result["success"] is False
        assert "nonexistent" in result["error"]

    def test_append_event_called(self):
        run = _mock_run(stdout="Switched to branch 'main'")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event') as mock_event:
                git_checkout("main")
        mock_event.assert_called_once()
        assert mock_event.call_args[0][1]["operation"] == "checkout"


class TestGitPull:
    def test_pull_success(self):
        run = _mock_run(stdout="Already up to date.")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_pull()
        assert result["success"] is True
        assert run.calls[0] == ["git", "pull"]

    def test_pull_with_rebase(self):
        run = _mock_run(stdout="Already up to date.")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_pull(rebase=True)
        assert result["success"] is True
        assert run.calls[0] == ["git", "pull", "--rebase"]

    def test_pull_failure(self):
        run = _mock_run(returncode=1, stdout="", stderr="no upstream configured")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_pull()
        assert result["success"] is False
        assert result["error"] == "no upstream configured"


class TestGitPush:
    def test_push_success(self):
        run = _mock_run(stdout="Everything up-to-date")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_push()
        assert result["success"] is True
        assert run.calls[0] == ["git", "push"]

    def test_push_failure(self):
        run = _mock_run(returncode=1, stdout="", stderr="rejected")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_push()
        assert result["success"] is False
        assert result["error"] == "rejected"


class TestGitDiff:
    def test_diff_success(self):
        run = _mock_run(stdout="--- a/file.txt\n+++ b/file.txt")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_diff()
        assert result["success"] is True
        assert "--- a/file.txt" in result["output"]

    def test_diff_no_changes(self):
        run = _mock_run(stdout="")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_diff()
        assert result["success"] is True
        assert result["output"] == ""

    def test_diff_failure(self):
        run = _mock_run(returncode=1, stdout="", stderr="not a git repository")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_diff()
        assert result["success"] is False


class TestGitCurrentBranch:
    def test_current_branch_success(self):
        run = _mock_run(stdout="main")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_current_branch()
        assert result["success"] is True
        assert result["output"] == "main"

    def test_current_branch_failure(self):
        run = _mock_run(returncode=1, stdout="", stderr="fatal: not a git repository")
        with patch('tools.git_tool.subprocess.run', side_effect=run):
            with patch('tools.git_tool.append_event'):
                result = git_current_branch()
        assert result["success"] is False


class TestGitIntegration:
    """Integration tests using a real git repository in the hermetic BASE_DIR.

    git_tool reads bootstrap.BASE_DIR at call time, and conftest points it at
    a per-test tmp dir — so a plain `git init` there is all the setup needed.
    """

    def _init_repo(self):
        base = str(bootstrap.BASE_DIR)
        subprocess.run(["git", "init", "-q"], cwd=base, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=base, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=base, capture_output=True)
        return Path(base)

    def test_commit_new_file(self):
        base = self._init_repo()
        (base / "test_file.txt").write_text("integration test content")

        result = git_commit("Add test file")
        assert result["success"] is True

        commit_log = subprocess.run(
            ["git", "log", "--oneline"], cwd=base, capture_output=True, text=True,
        )
        assert "Add test file" in commit_log.stdout

    def test_branch_switching(self):
        base = self._init_repo()
        (base / "initial.txt").write_text("initial")

        assert git_commit("Initial commit")["success"] is True
        assert git_branch("test-branch", action="create")["success"] is True
        assert git_checkout("test-branch")["success"] is True

        branch_status = git_current_branch()
        assert branch_status["success"] is True
        assert "test-branch" in branch_status["output"]

    def test_checkout_nonexistent_branch_integration(self):
        base = self._init_repo()
        (base / "initial.txt").write_text("initial")
        assert git_commit("Initial commit")["success"] is True

        result = git_checkout("nonexistent-branch-xyz")
        assert result["success"] is False
        assert result["error"] is not None

    def test_status_shows_untracked(self):
        base = self._init_repo()
        (base / "tracked.txt").write_text("content")
        assert git_commit("Initial commit")["success"] is True

        (base / "untracked.txt").write_text("untracked content")
        status = git_status()
        assert status["success"] is True
        assert "untracked.txt" in status["output"]

    def test_diff_shows_changes(self):
        base = self._init_repo()
        target = base / "diff_test.txt"
        target.write_text("original")
        assert git_commit("Initial commit")["success"] is True

        target.write_text("modified")
        diff = git_diff()
        assert diff["success"] is True
        assert "modified" in diff["output"]
