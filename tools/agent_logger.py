"""Agent Logger — structured logging for agent runs and opencode calls.

Logs are written to `<project_folder>/.sdlc/` for project-scoped agents,
or `~/dev-projects/sdlc-ai/sdlc-logs/` for non-project (global) agents.

Each agent run gets a JSONL file under `agents/<job_id>.jsonl`.
OpenCode calls get a JSONL file under `opencode_calls/<job_id>.jsonl`.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bootstrap import BASE_DIR

# Curated env var prefixes to capture (avoid leaking secrets)
_RELATIVE_ENV_PREFIXES = (
    "OPENCODE",
    "PULSE",
    "LLM",
    "MODEL",
    "PYTHONPATH",
    "SDLC",
    "API_KEY",
    "ENDPOINT",
    "PROVIDER",
    "TEMPERATURE",
)

# Fallback directory for agents not tied to a specific project
_GLOBAL_LOGS_DIR = Path("~/dev-projects/sdlc-ai/sdlc-logs").expanduser()

# User projects root
_USER_PROJECTS_ROOT = Path("~/dev-projects/sdlc-ai/user_projects").expanduser()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _collect_env_vars() -> Dict[str, str]:
    """Collect only the curated environment variables.

    Values of credential-bearing variables (API keys, tokens, secrets) are
    redacted — the variable *name* is useful for debugging, the value must
    never land in a log file.
    """
    try:
        from tools.secrets_tool import looks_secret, REDACTED
    except Exception:
        looks_secret, REDACTED = (lambda n: False), "***redacted***"

    env = {}
    for key, value in os.environ.items():
        for prefix in _RELATIVE_ENV_PREFIXES:
            if key.startswith(prefix):
                env[key] = REDACTED if looks_secret(key) else value
                break
    return env


def _find_project_folder(project_id: str) -> Optional[Path]:
    """Find the project folder by searching user_projects for matching id."""
    if not _USER_PROJECTS_ROOT.is_dir():
        return None

    # Try direct path first
    direct = _USER_PROJECTS_ROOT / project_id / "state.json"
    if direct.exists():
        return direct.parent

    # Search through all project directories
    for folder in _USER_PROJECTS_ROOT.iterdir():
        if folder.is_dir():
            state_file = folder / "state.json"
            if state_file.exists():
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("id") == project_id:
                        return folder
                except (json.JSONDecodeError, OSError):
                    continue
    return None


def resolve_sdlc_dir(project_id: Optional[str] = None) -> Path:
    """Resolve the .sdlc/ log directory for a given project.

    If project_id is provided, returns `<project_folder>/.sdlc/`.
    Otherwise returns `~/dev-projects/sdlc-ai/sdlc-logs/`.
    """
    if project_id:
        folder = _find_project_folder(project_id)
        if folder:
            sdlc = folder / ".sdlc"
            sdlc.mkdir(parents=True, exist_ok=True)
            return sdlc

    # Fallback to global logs
    _GLOBAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return _GLOBAL_LOGS_DIR


def _append_jsonl(filepath: Path, entry: Dict[str, Any]) -> None:
    """Append a JSON entry to a JSONL file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _agent_log_path(sdlc_dir: Path, job_id: str) -> Path:
    return sdlc_dir / "agents" / f"{job_id}.jsonl"


def _opencode_log_path(sdlc_dir: Path, job_id: str) -> Path:
    return sdlc_dir / "opencode_calls" / f"{job_id}.jsonl"


def log_agent_start(
    project_id: Optional[str],
    job_id: str,
    persona: str,
    system_prompt: str,
    args: Optional[Dict[str, Any]] = None,
    goal: Optional[str] = None,
    task_id: Optional[int] = None,
) -> Path:
    """Log the start of an agent run.

    Creates the agents/<job_id>.jsonl file with the first entry containing
    system prompt, arguments, and relevant environment variables.

    Returns the path to the log file.
    """
    sdlc_dir = resolve_sdlc_dir(project_id)
    log_path = _agent_log_path(sdlc_dir, job_id)

    entry = {
        "ts": _now(),
        "event": "start",
        "job_id": job_id,
        "persona": persona,
        "goal": goal,
        "task_id": task_id,
        "project_id": project_id,
        "system_prompt": system_prompt,
        "args": args or {},
        "env": _collect_env_vars(),
    }

    _append_jsonl(log_path, entry)
    return log_path


def log_agent_step(
    project_id: Optional[str],
    job_id: str,
    turn: int,
    phase: str,
    detail: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_params: Optional[Dict[str, Any]] = None,
    observation: Optional[str] = None,
) -> None:
    """Log a single step in the agent's reasoning loop.

    Phase values: "thinking", "action", "observation", "nudge"
    """
    sdlc_dir = resolve_sdlc_dir(project_id)
    log_path = _agent_log_path(sdlc_dir, job_id)

    entry: Dict[str, Any] = {
        "ts": _now(),
        "event": "step",
        "job_id": job_id,
        "turn": turn,
        "phase": phase,
    }

    if detail is not None:
        entry["detail"] = detail
    if tool_name is not None:
        entry["tool"] = tool_name
    if tool_params is not None:
        entry["tool_params"] = tool_params
    if observation is not None:
        entry["observation"] = observation

    _append_jsonl(log_path, entry)


def log_agent_complete(
    project_id: Optional[str],
    job_id: str,
    success: bool,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Log the completion or failure of an agent run."""
    sdlc_dir = resolve_sdlc_dir(project_id)
    log_path = _agent_log_path(sdlc_dir, job_id)

    entry: Dict[str, Any] = {
        "ts": _now(),
        "event": "complete",
        "job_id": job_id,
        "success": success,
    }
    if result is not None:
        entry["result"] = result
    if error is not None:
        entry["error"] = error

    _append_jsonl(log_path, entry)


def log_opencode_call(
    project_id: Optional[str],
    job_id: str,
    method: str,
    path: str,
    request: Optional[Dict[str, Any]] = None,
    response: Optional[Dict[str, Any]] = None,
    duration_ms: Optional[float] = None,
) -> None:
    """Log an exact OpenCode API call with parameters and response."""
    sdlc_dir = resolve_sdlc_dir(project_id)
    log_path = _opencode_log_path(sdlc_dir, job_id)

    entry: Dict[str, Any] = {
        "ts": _now(),
        "event": "opencode",
        "job_id": job_id,
        "method": method,
        "path": path,
    }
    if request is not None:
        entry["request"] = request
    if response is not None:
        entry["response"] = response
    if duration_ms is not None:
        entry["duration_ms"] = round(duration_ms, 2)

    _append_jsonl(log_path, entry)


def log_opencode_task(
    project_id: Optional[str],
    job_id: str,
    task_description: str,
    system_prompt: str,
    params: Dict[str, Any],
    response: Dict[str, Any],
    duration_ms: Optional[float] = None,
) -> None:
    """Log a high-level opencode_task call with all parameters and result."""
    sdlc_dir = resolve_sdlc_dir(project_id)
    log_path = _agent_log_path(sdlc_dir, job_id)

    entry: Dict[str, Any] = {
        "ts": _now(),
        "event": "opencode_task",
        "job_id": job_id,
        "task_description": task_description,
        "system_prompt": system_prompt,
        "params": params,
        "response": response,
    }
    if duration_ms is not None:
        entry["duration_ms"] = round(duration_ms, 2)

    _append_jsonl(log_path, entry)
