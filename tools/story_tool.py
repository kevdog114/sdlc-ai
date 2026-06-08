"""Story Tool — user story CRUD, auto-derived status, and progress tracking.

Stories are the primary unit of work. Each story contains a set of child tasks.
Story status is automatically derived from child task statuses:
  all done              → done
  any in architect_review → architect_review
  any in testing          → testing
  any in_progress         → in_progress
  otherwise               → backlog
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from bootstrap import (
    BASE_DIR,
    STATE_DIR,
    TASK_REGISTRY_PATH,
    load_json,
    save_json,
    append_event,
    get_task,
    load_project_state,
    save_project_state,
)

STORY_REGISTRY_PATH = STATE_DIR / "story_registry.json"

# Task statuses mapped to numeric weight for status derivation
# Higher weight = further along the pipeline
_STATUS_WEIGHT = {
    "backlog": 0,
    "pending": 0,
    "rejected": 0,
    "failed": 0,
    "escalated": 0,
    "blocked": 0,
    "in_progress": 1,
    "pending_verification": 2,
    "testing": 2,
    "testing_passed": 3,
    "architect_review": 3,
    "done": 4,
    "completed": 4,
}

# Story status columns
STORY_COLUMNS = ["backlog", "in_progress", "testing", "architect_review", "done"]

# Statuses that count as "active work" (not backlog)
_ACTIVE_STATUSES = {"in_progress", "pending_verification", "testing", "testing_passed", "architect_review"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_story_registry() -> Dict[str, Any]:
    return load_json(STORY_REGISTRY_PATH, {"version": "1.0.0", "stories": [], "next_id": 1})


def _save_story_registry(registry: Dict[str, Any]) -> None:
    save_json(STORY_REGISTRY_PATH, registry)


# ── CRUD ────────────────────────────────────────────────────────


def create_story(
    title: str,
    description: str = "",
    acceptance_criteria: Optional[List[str]] = None,
    priority: str = "medium",
) -> Dict[str, Any]:
    """Create a new user story and return it."""
    registry = _load_story_registry()
    story_num = registry.get("next_id", 1)
    registry["next_id"] = story_num + 1

    story_id = f"STORY-{story_num}"
    story = {
        "id": story_id,
        "title": title,
        "description": description,
        "acceptance_criteria": acceptance_criteria or [],
        "priority": priority if priority in ("high", "medium", "low") else "medium",
        "task_ids": [],
        "completion_notes": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    registry["stories"].append(story)
    _save_story_registry(registry)

    append_event(
        "tool:story",
        {"action": "create", "story_id": story_id, "title": title, "success": True},
    )
    return story


def get_story(story_id: Any) -> Optional[Dict[str, Any]]:
    """Look up a story by 'STORY-1' string or by integer."""
    registry = _load_story_registry()

    if isinstance(story_id, int):
        lookup = f"STORY-{story_id}"
    else:
        lookup = str(story_id)

    for story in registry["stories"]:
        if story["id"] == lookup:
            return story
    return None


def list_stories(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all stories, optionally filtered by derived status."""
    registry = _load_story_registry()
    stories = registry.get("stories", [])

    results = []
    for story in stories:
        derived = derive_story_status(story["id"])
        entry = {**story, "derived_status": derived}
        if status is None or derived == status:
            results.append(entry)

    append_event(
        "tool:story",
        {"action": "list", "count": len(results), "filter": status, "success": True},
    )
    return results


def update_story(
    story_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    acceptance_criteria: Optional[List[str]] = None,
    priority: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update mutable fields of an existing story."""
    registry = _load_story_registry()

    for story in registry["stories"]:
        if story["id"] == str(story_id):
            if title is not None:
                story["title"] = title
            if description is not None:
                story["description"] = description
            if acceptance_criteria is not None:
                story["acceptance_criteria"] = acceptance_criteria
            if priority is not None and priority in ("high", "medium", "low"):
                story["priority"] = priority
            story["updated_at"] = _now()

            _save_story_registry(registry)
            append_event(
                "tool:story",
                {"action": "update", "story_id": story_id, "success": True},
            )
            return story
    return None


def delete_story(story_id: str) -> bool:
    """Remove a story. Does NOT delete child tasks."""
    registry = _load_story_registry()
    original_len = len(registry["stories"])
    registry["stories"] = [s for s in registry["stories"] if s["id"] != str(story_id)]

    if len(registry["stories"]) < original_len:
        _save_story_registry(registry)
        append_event(
            "tool:story",
            {"action": "delete", "story_id": story_id, "success": True},
        )
        return True
    return False


# ── Task Linking ────────────────────────────────────────────────


def add_task_to_story(story_id: str, task_id: int) -> bool:
    """Link a task to a story. Updates both story and task records."""
    registry = _load_story_registry()

    for story in registry["stories"]:
        if story["id"] == str(story_id):
            if task_id not in story["task_ids"]:
                story["task_ids"].append(task_id)
                story["updated_at"] = _now()
                _save_story_registry(registry)

            # Update task's story_id field
            from bootstrap import TASK_REGISTRY_PATH as _tr
            task_reg = load_json(_tr, {})
            for task in task_reg.get("tasks", []):
                if task["id"] == task_id:
                    task["story_id"] = str(story_id)
                    save_json(_tr, task_reg)
                    # Update individual task file
                    task_file = BASE_DIR / "tasks" / f"task_{task_id}.json"
                    if task_file.exists():
                        save_json(task_file, task)
                    break

            append_event(
                "tool:story",
                {"action": "add_task", "story_id": story_id, "task_id": task_id, "success": True},
            )
            return True
    return False


def remove_task_from_story(story_id: str, task_id: int) -> bool:
    """Unlink a task from a story."""
    registry = _load_story_registry()

    for story in registry["stories"]:
        if story["id"] == str(story_id):
            if task_id in story["task_ids"]:
                story["task_ids"].remove(task_id)
                story["updated_at"] = _now()
                _save_story_registry(registry)

            # Clear task's story_id
            from bootstrap import TASK_REGISTRY_PATH as _tr
            task_reg = load_json(_tr, {})
            for task in task_reg.get("tasks", []):
                if task["id"] == task_id:
                    task["story_id"] = None
                    save_json(_tr, task_reg)
                    task_file = BASE_DIR / "tasks" / f"task_{task_id}.json"
                    if task_file.exists():
                        save_json(task_file, task)
                    break

            append_event(
                "tool:story",
                {"action": "remove_task", "story_id": story_id, "task_id": task_id, "success": True},
            )
            return True
    return False


def get_story_tasks(story_id: str) -> List[Dict[str, Any]]:
    """Return all child tasks of a story with full details."""
    story = get_story(story_id)
    if not story:
        return []

    tasks = []
    for tid in story.get("task_ids", []):
        task = get_task(tid)
        if task:
            tasks.append(task)
    return tasks


# ── Auto-Derived Status ────────────────────────────────────────


def derive_story_status(story_id: str) -> str:
    """Derive story status from child task statuses.

    Logic (evaluated top-down, first match wins):
      1. No tasks → backlog
      2. All tasks done/completed → done
      3. Any task in architect_review → architect_review
      4. Any task in testing/testing_passed/pending_verification → testing
      5. Any task in in_progress → in_progress
      6. Otherwise → backlog
    """
    story = get_story(story_id)
    if not story:
        return "backlog"

    task_ids = story.get("task_ids", [])
    if not task_ids:
        return "backlog"

    statuses: Set[str] = set()
    for tid in task_ids:
        task = get_task(tid)
        if task:
            statuses.add(task.get("status", "pending"))

    if not statuses:
        return "backlog"

    # All tasks done
    terminal = {"done", "completed"}
    if all(s in terminal for s in statuses):
        return "done"

    # Check for architect review
    if "architect_review" in statuses:
        return "architect_review"

    # Check for testing (includes testing_passed, pending_verification)
    if statuses & {"testing", "testing_passed", "pending_verification"}:
        return "testing"

    # Check for active work
    if statuses & _ACTIVE_STATUSES:
        return "in_progress"

    return "backlog"


def sync_all_stories() -> Dict[str, List[str]]:
    """Re-derive status for all stories and sync story kanban columns.

    Returns the resulting kanban board.
    """
    registry = _load_story_registry()
    board: Dict[str, List[str]] = {col: [] for col in STORY_COLUMNS}

    for story in registry.get("stories", []):
        status = derive_story_status(story["id"])
        # Map derived status to kanban column
        col = _status_to_column(status)
        if story["id"] not in board[col]:
            board[col].append(story["id"])

    # Persist story kanban in project state
    state = load_project_state()
    if "stories_kanban" not in state:
        state["stories_kanban"] = {col: [] for col in STORY_COLUMNS}
    state["stories_kanban"] = board
    save_project_state(state)

    append_event(
        "tool:story",
        {"action": "sync_all", "board": {k: len(v) for k, v in board.items()}},
    )
    return board


def _status_to_column(status: str) -> str:
    """Map a derived story status to a kanban column."""
    mapping = {
        "backlog": "backlog",
        "in_progress": "in_progress",
        "testing": "testing",
        "architect_review": "architect_review",
        "done": "done",
    }
    return mapping.get(status, "backlog")


# ── Progress ────────────────────────────────────────────────────


def get_story_progress(story_id: str) -> Dict[str, Any]:
    """Return progress metrics for a story.

    Returns:
        {
            story_id, title, total_tasks, done_tasks,
            in_progress_tasks, testing_tasks, percent,
            status, tasks (list of {id, description, status})
        }
    """
    story = get_story(story_id)
    if not story:
        return {"error": f"Story {story_id} not found"}

    tasks = get_story_tasks(story_id)
    total = len(tasks)
    done = sum(1 for t in tasks if t["status"] in ("done", "completed"))
    in_progress = sum(1 for t in tasks if t["status"] in _ACTIVE_STATUSES)
    testing = sum(1 for t in tasks if t["status"] in ("testing", "testing_passed", "pending_verification"))
    percent = round((done / total) * 100) if total > 0 else 0

    return {
        "story_id": story["id"],
        "title": story["title"],
        "total_tasks": total,
        "done_tasks": done,
        "in_progress_tasks": in_progress,
        "testing_tasks": testing,
        "percent": percent,
        "status": derive_story_status(story_id),
        "tasks": [
            {"id": t["id"], "description": t["description"], "status": t["status"]}
            for t in tasks
        ],
    }


def complete_story(story_id: str) -> Optional[Dict[str, Any]]:
    """Synthesize completion notes from all child tasks.

    Only succeeds if all child tasks are done.
    """
    story = get_story(story_id)
    if not story:
        return None

    tasks = get_story_tasks(story_id)
    all_done = all(t["status"] in ("done", "completed") for t in tasks)
    if not all_done:
        return None

    notes_parts = []
    for t in tasks:
        tid = t["id"]
        desc = t["description"]
        note = t.get("completion_notes") or "Completed."
        notes_parts.append(f"  Task #{tid} ({desc}): {note[:200]}")

    story["completion_notes"] = (
        f"Story completed at {_now()}\n"
        f"Tasks: {len(tasks)}\n"
        f"---\n" + "\n".join(notes_parts)
    )
    story["updated_at"] = _now()

    registry = _load_story_registry()
    for s in registry["stories"]:
        if s["id"] == story["id"]:
            s["completion_notes"] = story["completion_notes"]
            s["updated_at"] = story["updated_at"]
            break
    _save_story_registry(registry)

    append_event(
        "tool:story",
        {"action": "complete", "story_id": story_id, "success": True},
    )
    return story


# ── Story Kanban Board ─────────────────────────────────────────


def get_story_board_state() -> Dict[str, List[Dict[str, Any]]]:
    """Return the full story kanban board with story details per column."""
    sync_all_stories()
    state = load_project_state()
    skanban = state.get("stories_kanban", {})

    registry = _load_story_registry()
    stories_by_id = {s["id"]: s for s in registry.get("stories", [])}

    board: Dict[str, List[Dict[str, Any]]] = {}
    for col in STORY_COLUMNS:
        story_ids = skanban.get(col, [])
        entries = []
        for sid in story_ids:
            if sid in stories_by_id:
                s = stories_by_id[sid]
                progress = get_story_progress(sid)
                entries.append({**s, "derived_status": derive_story_status(sid), "progress": progress})
        board[col] = entries
    return board


def get_column_stories(column: str) -> List[Dict[str, Any]]:
    """Return all stories in a specific kanban column."""
    board = get_story_board_state()
    return board.get(column, [])
