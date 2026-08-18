"""Git Tool — high-level interface for repository version control operations.

Commands run as argv lists (never shell=True): commit messages and branch
names — which are LLM-authored and therefore attacker-influenceable — are
passed as arguments, not interpolated into a shell string.
"""

import subprocess
from typing import List, Optional

import bootstrap
from bootstrap import append_event

GIT_TIMEOUT = 120

# Branch names come from LLM output; constrain to git-plausible characters so
# a hostile "name" can't smuggle option-like or control garbage.
_BRANCH_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-"
)


def _valid_branch(name: str) -> bool:
    return bool(name) and not name.startswith("-") and all(c in _BRANCH_OK for c in name)


def _run_git(args: List[str], operation: str) -> dict:
    """Execute one git command (argv) in the project root."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(bootstrap.BASE_DIR),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        success = result.returncode == 0
        output = result.stdout.strip() if success else ""
        error = result.stderr.strip() if not success else None
    except subprocess.TimeoutExpired:
        success, output, error = False, "", f"git {operation} timed out after {GIT_TIMEOUT}s"
    except OSError as e:
        success, output, error = False, "", str(e)

    append_event(
        "tool:git_operation",
        {
            "operation": operation,
            "command": "git " + " ".join(args),
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
    return _run_git(["status", "--porcelain", "-b"], "status")


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

    add_result = _run_git(["add", "-A"], "commit_add")
    if not add_result["success"]:
        return add_result
    # -m message as its own argv element: quotes, backticks, $() are inert.
    return _run_git(["commit", "-m", message], "commit")


def git_branch(name: str = "", action: str = "create") -> dict:
    """Creates a new branch, switches to one, deletes one, or lists branches."""
    if action == "list":
        return _run_git(["branch"], "branch_list")

    if not _valid_branch(name):
        append_event(
            "tool:git_operation",
            {"operation": f"branch_{action}", "success": False, "error": f"Invalid branch name: {name!r}"},
        )
        return {"success": False, "output": "", "error": f"Invalid branch name: {name!r}"}

    if action == "create":
        return _run_git(["branch", name], "branch_create")
    elif action == "checkout":
        return _run_git(["checkout", name], "branch_checkout")
    elif action == "delete":
        return _run_git(["branch", "-d", name], "branch_delete")
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
    if not _valid_branch(branch_name):
        return {"success": False, "output": "", "error": f"Invalid branch name: {branch_name!r}"}
    return _run_git(["checkout", branch_name], "checkout")


def git_pull(rebase: bool = False) -> dict:
    """Synchronizes the local repository with the remote (pull)."""
    args = ["pull", "--rebase"] if rebase else ["pull"]
    return _run_git(args, "pull")


def git_push() -> dict:
    """Synchronizes the local repository with the remote (push)."""
    return _run_git(["push"], "push")


def git_diff() -> dict:
    """Returns the current unstaged changes for reasoning."""
    return _run_git(["diff"], "diff")


def git_current_branch() -> dict:
    """Returns the name of the current branch."""
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], "current_branch")


if __name__ == "__main__":
    print("[test] Current branch:", git_current_branch())
    print("[test] Status:", git_status())
    print("[test] Diff:", git_diff())
