#!/usr/bin/env python3
"""Phase 1 Seed Agent — bootstrap.py

Initializes the basic directory structure and JSON-based task registry
for the SDLC AI system.
"""


import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
AGENT_JOBS_DIR = STATE_DIR / "agent_jobs"
INTERFACE_SPECS_DIR = STATE_DIR / "interface_specs"
TASKS_DIR = BASE_DIR / "tasks"
LOGS_DIR = BASE_DIR / "logs"

TASK_REGISTRY_PATH = STATE_DIR / "task_registry.json"
STORY_REGISTRY_PATH = STATE_DIR / "story_registry.json"
STATE_FILE_PATH = STATE_DIR / "project_state.json"
EVENT_LOG_PATH = LOGS_DIR / "event_log.jsonl"
CLARIFICATION_PATH = STATE_DIR / "clarifications.json"
LLM_LOG_DIR = BASE_DIR / "llm_log"


def ensure_dirs():
    """Create the required directory structure."""
    for d in (TASKS_DIR, LOGS_DIR, STATE_DIR, AGENT_JOBS_DIR, INTERFACE_SPECS_DIR, LLM_LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ── Task Registry ───────────────────────────────────────────────

def init_task_registry() -> Dict[str, Any]:
    """Create (or return existing) the master task registry."""
    if TASK_REGISTRY_PATH.exists():
        return load_json(TASK_REGISTRY_PATH, {})

    registry: Dict[str, Any] = {
        "version": "1.0.0",
        "created_at": _now(),
        "tasks": [],
        "next_id": 1,
    }
    save_json(TASK_REGISTRY_PATH, registry)
    return registry


def add_task(
    description: str,
    agent: str = "unassigned",
    status: str = "pending",
    dependencies: Optional[List[int]] = None,
    story_id: Optional[str] = None,
    interface_spec_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a new task to the registry and return it."""
    registry = load_json(TASK_REGISTRY_PATH, {})
    task_id = registry.get("next_id", 1)
    registry["next_id"] = task_id + 1

    task = {
        "id": task_id,
        "uuid": str(uuid4()),
        "description": description,
        "agent": agent,
        "status": status,
        "dependencies": dependencies or [],
        "story_id": story_id,
        "interface_spec_id": interface_spec_id,
        "project_id": project_id,
        "created_at": _now(),
        "updated_at": _now(),
        "verification_artifacts": [],
        "completion_notes": None,
    }
    registry["tasks"].append(task)
    save_json(TASK_REGISTRY_PATH, registry)

    # Also write an individual task file under tasks/
    task_file = TASKS_DIR / f"task_{task_id}.json"
    save_json(task_file, task)

    return task


def update_task_status(task_id: int, status: str, notes: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Update a task's status, optionally attach notes, and sync kanban column."""
    registry = load_json(TASK_REGISTRY_PATH, {})
    for task in registry["tasks"]:
        if task["id"] == task_id:
            old_status = task["status"]
            task["status"] = status
            task["updated_at"] = _now()
            if notes:
                task["completion_notes"] = notes
            save_json(TASK_REGISTRY_PATH, registry)

            task_file = TASKS_DIR / f"task_{task_id}.json"
            save_json(task_file, task)

            # Sync kanban column on status change
            if old_status != status:
                try:
                    from tools.kanban_tool import sync_task_column
                    sync_task_column(task_id, status)
                except ImportError:
                    pass

            append_event(
                "system:task_update",
                {
                    "task_id": task_id,
                    "old_status": old_status,
                    "new_status": status,
                },
            )
            return task
    return None


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve a single task by ID."""
    registry = load_json(TASK_REGISTRY_PATH, {})
    for task in registry["tasks"]:
        if task["id"] == task_id:
            return task
    return None


def list_tasks(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all tasks, optionally filtered by status."""
    registry = load_json(TASK_REGISTRY_PATH, {})
    tasks = registry.get("tasks", [])
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    return tasks


# ── Story Registry ──────────────────────────────────────────────

def init_story_registry() -> Dict[str, Any]:
    """Create (or return existing) the story registry."""
    if STORY_REGISTRY_PATH.exists():
        return load_json(STORY_REGISTRY_PATH, {})

    registry: Dict[str, Any] = {
        "version": "1.0.0",
        "stories": [],
        "next_id": 1,
    }
    save_json(STORY_REGISTRY_PATH, registry)
    return registry


# ── Clarification Registry ──────────────────────────────────────

def init_clarification_registry() -> Dict[str, Any]:
    """Create (or return existing) the clarification registry."""
    if CLARIFICATION_PATH.exists():
        return load_json(CLARIFICATION_PATH, {})

    registry: Dict[str, Any] = {
        "version": "1.0.0",
        "requests": [],
        "next_id": 1,
    }
    save_json(CLARIFICATION_PATH, registry)
    return registry


# ── Full Initialization ─────────────────────────────────────────

def ensure_initialized() -> None:
    """Ensure all required directories and state files exist.

    Safe to call multiple times. Creates missing directories and
    initializes any state files that don't yet exist.
    """
    ensure_dirs()
    init_task_registry()
    init_story_registry()
    init_clarification_registry()
    init_project_state()
    if not EVENT_LOG_PATH.exists():
        EVENT_LOG_PATH.touch()


# ── Event Log (JSONL) ───────────────────────────────────────────

def append_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Append a structured event to the JSONL event log."""
    entry = {
        "timestamp": _now(),
        "type": event_type,
        **payload,
    }
    with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ── Project State ───────────────────────────────────────────────

def load_project_state() -> Dict[str, Any]:
    """Load the project state from the central JSON file."""
    return load_json(STATE_FILE_PATH, {"version": "1.0.0", "active_agents": {}})


def save_project_state(state: Dict[str, Any]) -> None:
    """Save the current project state to the central JSON file."""
    save_json(STATE_FILE_PATH, state)


def init_project_state(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Initialize the project state file with optional config."""
    if STATE_FILE_PATH.exists():
        return load_project_state()

    state: Dict[str, Any] = {
        "version": "1.0.0",
        "initialized_at": _now(),
        "human_vision": "",
        "technical_requirements": [],
        "system_architecture": {},
        "interface_specs": {},
        "active_spec_id": None,
        "project_config": config or {},
        "kanban": {
            "backlog": [],
            "in_progress": [],
            "testing": [],
            "architect_review": [],
            "done": [],
        },
        "stories_kanban": {
            "backlog": [],
            "in_progress": [],
            "testing": [],
            "architect_review": [],
            "done": [],
            "failed": [],
        },
        "active_agents": {}
    }
    save_json(STATE_FILE_PATH, state)
    return state


# ── Bootstrap ───────────────────────────────────────────────────

def bootstrap():
    """Run the full Phase 1 bootstrap sequence."""
    print("[bootstrap] Creating directory structure …")
    ensure_dirs()

    print("[bootstrap] Initializing task registry …")
    registry = init_task_registry()
    print(f"[bootstrap] Task registry ready at {TASK_REGISTRY_PATH}")

    print("[bootstrap] Initializing story registry …")
    story_reg = init_story_registry()
    print(f"[bootstrap] Story registry ready at {STORY_REGISTRY_PATH}")

    print("[bootstrap] Initializing project state …")
    state = init_project_state()
    print(f"[bootstrap] Project state ready at {STATE_FILE_PATH}")

    print("[bootstrap] Event log ready at …")
    print(f"[bootstrap]   {EVENT_LOG_PATH}")

    append_event("system:boot", {"phase": "seed_agent", "component": "bootstrap"})
    print("[bootstrap] Seed agent bootstrap complete.")
    return True


if __name__ == "__main__":
    bootstrap()
