"""Kanban Tool — manages task transitions between board columns.

Maps task status to kanban columns, moves tasks on status changes,
and provides board state queries for the dashboard.
"""

import json
from typing import Any, Dict, List, Optional

from bootstrap import (
    load_project_state,
    save_project_state,
    load_json,
    TASK_REGISTRY_PATH,
    append_event,
)

# Status -> kanban column mapping
STATUS_TO_COLUMN = {
    "pending": "backlog",
    "blocked": "backlog",
    "blocked_interface_gap": "backlog",
    "in_progress": "in_progress",
    "pending_verification": "testing",
    "testing": "testing",
    "testing_passed": "architect_review",
    "architect_review": "architect_review",
    "done": "done",
    "completed": "done",
    "failed": "backlog",
    "rejected": "backlog",
    "escalated": "backlog",
}

COLUMN_ORDER = ["backlog", "in_progress", "testing", "architect_review", "done"]


def get_column_for_status(status: str) -> str:
    """Return the kanban column a task should be in for a given status."""
    return STATUS_TO_COLUMN.get(status, "backlog")


def move_task_to_column(task_id: int, column: str) -> bool:
    """Move a specific task into a kanban column.

    Removes the task from all other columns first.
    """
    state = load_project_state()
    kanban = state.get("kanban", {})

    if column not in kanban:
        kanban[column] = []

    # Remove from all columns
    for col in kanban:
        kanban[col] = [tid for tid in kanban[col] if tid != task_id]

    # Add to target column
    if task_id not in kanban[column]:
        kanban[column].append(task_id)

    state["kanban"] = kanban
    save_project_state(state)

    append_event(
        "tool:kanban",
        {"action": "move_task", "task_id": task_id, "column": column},
    )
    return True


def sync_task_column(task_id: int, status: str) -> str:
    """Move a task to the correct column based on its status.

    Returns the column name the task was moved to.
    """
    column = get_column_for_status(status)
    move_task_to_column(task_id, column)
    return column


def sync_all_tasks() -> Dict[str, List[int]]:
    """Re-sync all kanban columns from the task registry.

    Reads every task's status and places it in the correct column.
    Returns the resulting kanban board state.
    """
    registry = load_json(TASK_REGISTRY_PATH, {})
    tasks = registry.get("tasks", [])

    board: Dict[str, List[int]] = {col: [] for col in COLUMN_ORDER}
    for task in tasks:
        tid = task.get("id")
        status = task.get("status", "pending")
        column = get_column_for_status(status)
        if tid and column in board:
            board[column].append(tid)

    state = load_project_state()
    state["kanban"] = board
    save_project_state(state)

    append_event(
        "tool:kanban",
        {"action": "sync_all", "board_counts": {k: len(v) for k, v in board.items()}},
    )
    return board


def get_board_state() -> Dict[str, List[Dict[str, Any]]]:
    """Return the full kanban board with task details per column.

    Returns:
        Dict mapping column name to list of task dicts.
    """
    state = load_project_state()
    kanban = state.get("kanban", {})

    registry = load_json(TASK_REGISTRY_PATH, {})
    tasks_by_id = {t["id"]: t for t in registry.get("tasks", [])}

    board: Dict[str, List[Dict[str, Any]]] = {}
    for col in COLUMN_ORDER:
        task_ids = kanban.get(col, [])
        board[col] = [tasks_by_id[tid] for tid in task_ids if tid in tasks_by_id]

    return board


def get_column_tasks(column: str) -> List[Dict[str, Any]]:
    """Return all tasks in a specific kanban column."""
    board = get_board_state()
    return board.get(column, [])


def transfer_task(task_id: int, from_column: str, to_column: str) -> bool:
    """Manually move a task from one column to another.

    Also updates the task's status to match the target column.
    """
    if from_column not in COLUMN_ORDER or to_column not in COLUMN_ORDER:
        return False

    # Reverse-map column -> canonical status
    column_status_map = {
        "backlog": "pending",
        "in_progress": "in_progress",
        "testing": "testing",
        "architect_review": "architect_review",
        "done": "done",
    }

    move_task_to_column(task_id, to_column)

    new_status = column_status_map.get(to_column, "pending")
    if new_status:
        from bootstrap import update_task_status
        update_task_status(task_id, new_status)

    return True
