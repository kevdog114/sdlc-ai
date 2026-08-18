"""Shared pytest configuration: hermetic, isolated state for every test.

The system keeps its state in JSON files whose locations are module-level
globals in ``bootstrap`` (and a handful of derived copies captured at import
time in ``tools/*``). Historically each test file mutated those globals in its
own ``setup_method`` and never restored them, so state leaked from one test
file into the next — the cause of the bulk of the suite's spurious failures.

This conftest makes the suite hermetic on two levels:

1. At *import* time it points every state path at a session-scoped temp
   directory. This catches import-time side effects — notably ``pulse_server``,
   which calls ``bootstrap.ensure_initialized()`` at module load — so nothing is
   ever written into the real repository.
2. Per test, an autouse fixture further redirects state into a fresh
   ``tmp_path`` and initializes an empty store there, restoring the
   session-scoped paths afterward.
"""

import atexit
import importlib
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bootstrap

# --- bootstrap path globals -------------------------------------------------
# Functions such as append_event/load_json/save_json/add_task read these
# globals at call time, so redirecting bootstrap alone covers every module that
# imported those *functions*. Only modules that imported a path *value* need
# their private copy redirected as well (see _DEP_ATTRS below).


def _point_bootstrap_at(base: Path) -> None:
    state = base / "state"
    bootstrap.BASE_DIR = base
    bootstrap.STATE_DIR = state
    bootstrap.AGENT_JOBS_DIR = state / "agent_jobs"
    bootstrap.INTERFACE_SPECS_DIR = state / "interface_specs"
    bootstrap.TASKS_DIR = base / "tasks"
    bootstrap.LOGS_DIR = base / "logs"
    bootstrap.TASK_REGISTRY_PATH = state / "task_registry.json"
    bootstrap.STORY_REGISTRY_PATH = state / "story_registry.json"
    bootstrap.STATE_FILE_PATH = state / "project_state.json"
    bootstrap.EVENT_LOG_PATH = base / "logs" / "event_log.jsonl"
    bootstrap.CLARIFICATION_PATH = state / "clarifications.json"
    bootstrap.LLM_LOG_DIR = base / "llm_log"


# --- dependent modules that captured a path value at import -----------------
# ROLES_DIR (orchestrator_tool) and agent_logger are intentionally left alone:
# tests need the real roles/ directory, and agent_logger has its own paths.
_DEP_ATTRS = {
    "tools.story_tool": ("BASE_DIR", "STATE_DIR", "STORY_REGISTRY_PATH"),
    "tools.interface_tool": ("BASE_DIR", "INTERFACE_SPECS_DIR"),
    "tools.knowledge_tool": ("BASE_DIR", "KNOWLEDGE_DIR"),
    "tools.clarification_tool": ("CLARIFICATION_PATH",),
    "tools.command_center_tool": ("BASE_DIR", "PID_FILE"),
    "tools.llm_logger": ("LLM_LOG_DIR",),
    # USER_PROJECTS_ROOT is a hardcoded ~/dev-projects path; redirect it so
    # project tests don't touch the real home directory.
    "tools.project_tool": ("USER_PROJECTS_ROOT",),
}


def _load_dependents():
    mods = {}
    for name in _DEP_ATTRS:
        try:
            mods[name] = importlib.import_module(name)
        except Exception:
            pass
    return mods


_DEP_MODS = _load_dependents()


def _point_dependents_at(base: Path) -> None:
    state = base / "state"
    values = {
        "tools.story_tool": {
            "BASE_DIR": base, "STATE_DIR": state,
            "STORY_REGISTRY_PATH": state / "story_registry.json",
        },
        "tools.interface_tool": {
            "BASE_DIR": base, "INTERFACE_SPECS_DIR": state / "interface_specs",
        },
        "tools.knowledge_tool": {
            "BASE_DIR": base, "KNOWLEDGE_DIR": base / ".knowledge",
        },
        "tools.clarification_tool": {
            "CLARIFICATION_PATH": state / "clarifications.json",
        },
        "tools.command_center_tool": {
            "BASE_DIR": base, "PID_FILE": state / "command_center.pid",
        },
        "tools.llm_logger": {"LLM_LOG_DIR": base / "llm_log"},
        "tools.project_tool": {"USER_PROJECTS_ROOT": base / "user_projects"},
    }
    for name, mod in _DEP_MODS.items():
        for attr, value in values.get(name, {}).items():
            setattr(mod, attr, value)


def _redirect_all(base: Path) -> None:
    _point_bootstrap_at(base)
    _point_dependents_at(base)


# --- session-scoped redirect (runs at conftest import, before collection) ---
# Points all state at a throwaway dir so import-time side effects (e.g.
# pulse_server calling ensure_initialized at module load) never touch the repo.
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="sdlcai-tests-"))
_redirect_all(_SESSION_DIR)
atexit.register(lambda: shutil.rmtree(_SESSION_DIR, ignore_errors=True))


@pytest.fixture(autouse=True)
def isolate_state(tmp_path):
    """Redirect all state into a per-test tmp dir and initialize it."""
    _redirect_all(tmp_path)
    bootstrap.ensure_initialized()
    try:
        yield tmp_path
    finally:
        # Return to the session temp dir (never the real repo) between tests.
        _redirect_all(_SESSION_DIR)
