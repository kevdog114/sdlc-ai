"""Registry Manager Tool — high-level interface for task registry operations."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from bootstrap import (
    TASK_REGISTRY_PATH,
    add_task,
    append_event,
    load_json,
    save_json,
)


def get_pending_tasks() -> List[Dict[str, Any]]:
    """Return all tasks with pending status."""
    return list_tasks(status="pending")


def list_tasks(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all tasks, optionally filtered by status."""
    try:
        registry = load_json(TASK_REGISTRY_PATH, {})
        tasks = registry.get("tasks", [])
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        append_event(
            "tool:registry_manager",
            {"action": "list_tasks", "count": len(tasks), "status": status, "success": True},
        )
        return tasks
    except Exception as e:
        append_event(
            "tool:registry_manager",
            {"action": "list_tasks", "success": False, "error": str(e)},
        )
        return []


def get_task_by_id(task_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve a single task by its integer ID."""
    try:
        registry = load_json(TASK_REGISTRY_PATH, {})
        for task in registry.get("tasks", []):
            if task["id"] == task_id:
                append_event(
                    "tool:registry_manager",
                    {"action": "get_by_id", "task_id": task_id, "success": True},
                )
                return task
        append_event(
            "tool:registry_manager",
            {"action": "get_by_id", "task_id": task_id, "success": False, "error": "not_found"},
        )
        return None
    except Exception as e:
        append_event(
            "tool:registry_manager",
            {"action": "get_by_id", "task_id": task_id, "success": False, "error": str(e)},
        )
        return None


def update_task_status(task_id: int, new_status: str, notes: str = "") -> bool:
    """Update the status (and optional notes) of an existing task."""
    try:
        registry = load_json(TASK_REGISTRY_PATH, {})
        for task in registry.get("tasks", []):
            if task["id"] == task_id:
                task["status"] = new_status
                if notes:
                    task["completion_notes"] = notes
                save_json(TASK_REGISTRY_PATH, registry)
                append_event(
                    "tool:registry_manager",
                    {
                        "action": "update_status",
                        "task_id": task_id,
                        "new_status": new_status,
                        "success": True,
                    },
                )
                return True
        append_event(
            "tool:registry_manager",
            {
                "action": "update_status",
                "task_id": task_id,
                "new_status": new_status,
                "success": False,
                "error": "not_found",
            },
        )
        return False
    except Exception as e:
        append_event(
            "tool:registry_manager",
            {
                "action": "update_status",
                "task_id": task_id,
                "new_status": new_status,
                "success": False,
                "error": str(e),
            },
        )
        return False


def create_new_task(
    description: str,
    agent: str = "unassigned",
    story_id: Optional[str] = None,
    interface_spec_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new task via bootstrap.add_task and return the created task."""
    try:
        task = add_task(
            description=description,
            agent=agent,
            story_id=story_id,
            interface_spec_id=interface_spec_id,
        )
        append_event(
            "tool:registry_manager",
            {"action": "create_task", "task_id": task["id"], "success": True},
        )
        return task
    except Exception as e:
        append_event(
            "tool:registry_manager",
            {"action": "create_task", "description": description, "success": False, "error": str(e)},
        )
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    print("[test] Pending tasks:", get_pending_tasks())
    print("[test] Task #1:", get_task_by_id(1))
    print("[test] Create task:", create_new_task("registry_manager smoke test", agent="system", story_id=None))
