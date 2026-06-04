"""Git Tool — high-level interface for repository version control operations."""

from typing import Optional

from bootstrap import BASE_DIR, append_event
from tools.shell_executor import execute_command


def _run_git(command: str, operation: str) -> dict:
    """Execute a git command within the project root and return a structured response."""
    full_command = f"cd {BASE_DIR} && {command}"
    result = execute_command(full_command)

    success = result["exit_code"] == 0
    output = result["stdout"] if success else ""
    error = result["stderr"] if not success else None

    append_event(
        "tool:git_operation",
        {
            "operation": operation,
            "command": command,
            "success": success,
            "error": error,
        },
    )

    return {
        "success": success,
        "output": output,
        "error": error,
    }


def git_status() -> dict:
    """Returns the current branch and file status (staged, unstaged, untracked)."""
    return _run_git("git status --porcelain -b", "status")


def git_commit(message: str) -> dict:
    """Stages all changes and commits them with the provided message."""
    if not message or not message.strip():
        append_event(
            "tool:git_operation",
            {"operation": "commit", "success": False, "error": "Commit message cannot be empty"},
        )
        return {
            "success": False,
            "output": "",
            "error": "Commit message cannot be empty",
        }

    result = _run_git(f'git add . && git commit -m "{message}"', "commit")
    return result


def git_branch(name: str, action: str = "create") -> dict:
    """Creates a new branch or switches to an existing one."""
    if action == "create":
        return _run_git(f"git branch {name}", "branch_create")
    elif action == "checkout":
        return _run_git(f"git checkout {name}", "branch_checkout")
    elif action == "delete":
        return _run_git(f"git branch -d {name}", "branch_delete")
    elif action == "list":
        return _run_git("git branch", "branch_list")
    else:
        append_event(
            "tool:git_operation",
            {"operation": "branch", "success": False, "error": f"Unknown branch action: {action}"},
        )
        return {
            "success": False,
            "output": "",
            "error": f"Unknown branch action: {action}",
        }


def git_checkout(branch_name: str) -> dict:
    """Switches to a specific branch."""
    return _run_git(f"git checkout {branch_name}", "checkout")


def git_pull(rebase: bool = False) -> dict:
    """Synchronizes the local repository with the remote (pull)."""
    flag = "--rebase" if rebase else ""
    return _run_git(f"git pull {flag}".strip(), "pull")


def git_push() -> dict:
    """Synchronizes the local repository with the remote (push)."""
    return _run_git("git push", "push")


def git_diff() -> dict:
    """Returns the current unstaged changes for reasoning."""
    return _run_git("git diff", "diff")


def git_current_branch() -> dict:
    """Returns the name of the current branch."""
    return _run_git("git rev-parse --abbrev-ref HEAD", "current_branch")


if __name__ == "__main__":
    print("[test] Current branch:", git_current_branch())
    print("[test] Status:", git_status())
    print("[test] Diff:", git_diff())
