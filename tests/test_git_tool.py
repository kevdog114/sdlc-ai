import sys
import os
import tempfile
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def _mock_success(stdout: str = "success output") -> dict:
    return {"exit_code": 0, "stdout": stdout, "stderr": ""}


def _mock_failure(stderr: str = "error output") -> dict:
    return {"exit_code": 1, "stdout": "", "stderr": stderr}


class TestGitStatus:
    def test_status_success(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("## main\n M tools/git_tool.py\n")
            with patch('tools.git_tool.append_event'):
                result = git_status()
            assert result["success"] is True
            assert result["error"] is None
            assert "## main" in result["output"]

    def test_status_with_no_changes(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("## main\n")
            with patch('tools.git_tool.append_event'):
                result = git_status()
            assert result["success"] is True

    def test_status_failure(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_failure("not a git repository")
            with patch('tools.git_tool.append_event'):
                result = git_status()
            assert result["success"] is False
            assert result["error"] == "not a git repository"
            assert result["output"] == ""

    def test_append_event_called(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("")
            with patch('tools.git_tool.append_event') as mock_event:
                git_status()
                mock_event.assert_called_once()
                assert mock_event.call_args[0][0] == "tool:git_operation"
                assert mock_event.call_args[0][1]["operation"] == "status"
                assert mock_event.call_args[0][1]["success"] is True


class TestGitCommit:
    def test_commit_success(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("[main abc123] Test commit\n")
            with patch('tools.git_tool.append_event'):
                result = git_commit("Test commit")
            assert result["success"] is True
            assert "Test commit" in mock_exec.call_args[0][0]

    def test_commit_empty_message(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            with patch('tools.git_tool.append_event'):
                result = git_commit("")
            assert result["success"] is False
            assert "empty" in result["error"].lower()
            mock_exec.assert_not_called()

    def test_commit_whitespace_only_message(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            with patch('tools.git_tool.append_event'):
                result = git_commit("   ")
            assert result["success"] is False
            mock_exec.assert_not_called()

    def test_commit_failure(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_failure("nothing to commit")
            with patch('tools.git_tool.append_event'):
                result = git_commit("Test commit")
            assert result["success"] is False
            assert result["error"] == "nothing to commit"

    def test_append_event_called(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("[main abc123] Test\n")
            with patch('tools.git_tool.append_event') as mock_event:
                git_commit("Test commit")
                mock_event.assert_called_once()
                assert mock_event.call_args[0][1]["operation"] == "commit"


class TestGitBranch:
    def test_branch_create_success(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("")
            with patch('tools.git_tool.append_event'):
                result = git_branch("feature-test")
            assert result["success"] is True
            assert "git branch feature-test" in mock_exec.call_args[0][0]

    def test_branch_create_default_action(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("")
            with patch('tools.git_tool.append_event'):
                result = git_branch("feature-test")
            assert result["success"] is True
            assert "git branch feature-test" in mock_exec.call_args[0][0]

    def test_branch_checkout_action(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("Switched to branch 'feature-test'")
            with patch('tools.git_tool.append_event'):
                result = git_branch("feature-test", action="checkout")
            assert result["success"] is True
            assert "git checkout feature-test" in mock_exec.call_args[0][0]

    def test_branch_delete_action(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("Deleted branch feature-test")
            with patch('tools.git_tool.append_event'):
                result = git_branch("feature-test", action="delete")
            assert result["success"] is True
            assert "git branch -d feature-test" in mock_exec.call_args[0][0]

    def test_branch_list_action(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("  main\n* feature-test")
            with patch('tools.git_tool.append_event'):
                result = git_branch("list", action="list")
            assert result["success"] is True
            assert "git branch" in mock_exec.call_args[0][0]

    def test_branch_unknown_action(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            with patch('tools.git_tool.append_event'):
                result = git_branch("test", action="rename")
            assert result["success"] is False
            assert "Unknown branch action" in result["error"]
            mock_exec.assert_not_called()

    def test_branch_create_failure(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_failure("branch already exists")
            with patch('tools.git_tool.append_event'):
                result = git_branch("existing-branch")
            assert result["success"] is False
            assert result["error"] == "branch already exists"


class TestGitCheckout:
    def test_checkout_success(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("Switched to branch 'main'")
            with patch('tools.git_tool.append_event'):
                result = git_checkout("main")
            assert result["success"] is True
            assert "git checkout main" in mock_exec.call_args[0][0]

    def test_checkout_nonexistent_branch(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_failure("error: pathspec 'nonexistent' did not match")
            with patch('tools.git_tool.append_event'):
                result = git_checkout("nonexistent")
            assert result["success"] is False
            assert "nonexistent" in result["error"]

    def test_append_event_called(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("Switched to branch 'main'")
            with patch('tools.git_tool.append_event') as mock_event:
                git_checkout("main")
                mock_event.assert_called_once()
                assert mock_event.call_args[0][1]["operation"] == "checkout"


class TestGitPull:
    def test_pull_success(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("Already up to date.")
            with patch('tools.git_tool.append_event'):
                result = git_pull()
            assert result["success"] is True
            assert "git pull" in mock_exec.call_args[0][0]

    def test_pull_with_rebase(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("Already up to date.")
            with patch('tools.git_tool.append_event'):
                result = git_pull(rebase=True)
            assert result["success"] is True
            assert "git pull --rebase" in mock_exec.call_args[0][0]

    def test_pull_failure(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_failure("no upstream configured")
            with patch('tools.git_tool.append_event'):
                result = git_pull()
            assert result["success"] is False
            assert result["error"] == "no upstream configured"


class TestGitPush:
    def test_push_success(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("Everything up-to-date")
            with patch('tools.git_tool.append_event'):
                result = git_push()
            assert result["success"] is True
            assert "git push" in mock_exec.call_args[0][0]

    def test_push_failure(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_failure("rejected")
            with patch('tools.git_tool.append_event'):
                result = git_push()
            assert result["success"] is False
            assert result["error"] == "rejected"


class TestGitDiff:
    def test_diff_success(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("--- a/file.txt\n+++ b/file.txt")
            with patch('tools.git_tool.append_event'):
                result = git_diff()
            assert result["success"] is True
            assert "--- a/file.txt" in result["output"]

    def test_diff_no_changes(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("")
            with patch('tools.git_tool.append_event'):
                result = git_diff()
            assert result["success"] is True
            assert result["output"] == ""

    def test_diff_failure(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_failure("not a git repository")
            with patch('tools.git_tool.append_event'):
                result = git_diff()
            assert result["success"] is False


class TestGitCurrentBranch:
    def test_current_branch_success(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_success("main")
            with patch('tools.git_tool.append_event'):
                result = git_current_branch()
            assert result["success"] is True
            assert result["output"] == "main"

    def test_current_branch_failure(self):
        with patch('tools.git_tool.execute_command') as mock_exec:
            mock_exec.return_value = _mock_failure("fatal: not a git repository")
            with patch('tools.git_tool.append_event'):
                result = git_current_branch()
            assert result["success"] is False


class TestGitIntegration:
    """Integration tests using a temporary git repository."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        subprocess.run(["git", "init", self.tmpdir], capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.tmpdir, capture_output=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_commit_new_file(self):
        test_file = Path(self.tmpdir) / "test_file.txt"
        test_file.write_text("integration test content")

        with patch('tools.git_tool.BASE_DIR', new=self.tmpdir):
            with patch('tools.git_tool.append_event'):
                add_result = git_commit("Add test file")
                assert add_result["success"] is True

        commit_log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=self.tmpdir,
            capture_output=True,
            text=True,
        )
        assert "Add test file" in commit_log.stdout

    def test_branch_switching(self):
        test_file = Path(self.tmpdir) / "initial.txt"
        test_file.write_text("initial")

        with patch('tools.git_tool.BASE_DIR', new=self.tmpdir):
            with patch('tools.git_tool.append_event'):
                add_result = git_commit("Initial commit")
                assert add_result["success"] is True

                branch_result = git_branch("test-branch", action="create")
                assert branch_result["success"] is True

                checkout_result = git_checkout("test-branch")
                assert checkout_result["success"] is True

                branch_status = git_current_branch()
                assert branch_status["success"] is True
                assert "test-branch" in branch_status["output"]

    def test_checkout_nonexistent_branch_integration(self):
        test_file = Path(self.tmpdir) / "initial.txt"
        test_file.write_text("initial")

        with patch('tools.git_tool.BASE_DIR', new=self.tmpdir):
            with patch('tools.git_tool.append_event'):
                add_result = git_commit("Initial commit")
                assert add_result["success"] is True

                checkout_result = git_checkout("nonexistent-branch-xyz")
                assert checkout_result["success"] is False
                assert checkout_result["error"] is not None

    def test_status_shows_untracked(self):
        test_file = Path(self.tmpdir) / "untracked.txt"
        test_file.write_text("untracked content")

        with patch('tools.git_tool.BASE_DIR', new=self.tmpdir):
            with patch('tools.git_tool.append_event'):
                add_result = git_commit("Initial commit")
                assert add_result["success"] is True

                status_result = git_status()
                assert status_result["success"] is True

    def test_diff_shows_changes(self):
        test_file = Path(self.tmpdir) / "diff_test.txt"
        test_file.write_text("original")

        with patch('tools.git_tool.BASE_DIR', new=self.tmpdir):
            with patch('tools.git_tool.append_event'):
                add_result = git_commit("Initial commit")
                assert add_result["success"] is True

                test_file.write_text("modified")

                diff_result = git_diff()
                assert diff_result["success"] is True
                assert "modified" in diff_result["output"]
