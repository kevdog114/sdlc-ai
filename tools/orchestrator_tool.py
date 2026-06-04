"""Orchestrator Tool — multi-agent task decomposition, delegation, and monitoring.

Provides the core logic for breaking high-level goals into atomic subtasks,
assigning them to specialist agent personas, executing them via the LLM,
and monitoring for failures with automatic retry/re-assign/escalate recovery.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bootstrap import (
    BASE_DIR,
    add_task,
    append_event,
    get_task,
    list_tasks,
    update_task_status,
)

from tools.knowledge_tool import store_insight
from tools.llm_tool import query_llm
from tools.registry_tool import create_new_task, get_task_by_id, update_task_status as registry_update

ROLES_DIR = BASE_DIR / "roles"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_ESCALATED = "escalated"

MAX_RETRIES = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Role Management ─────────────────────────────────────────────


def list_roles() -> List[str]:
    """Return the names of all available agent personas."""
    if not ROLES_DIR.is_dir():
        return []
    return [f.stem for f in sorted(ROLES_DIR.glob("*.json"))]


def load_role(role_name: str) -> Optional[Dict[str, Any]]:
    """Load an agent persona definition by name.

    Returns the parsed JSON dict, or None if the role file doesn't exist.
    """
    role_path = ROLES_DIR / f"{role_name}.json"
    if not role_path.is_file():
        return None
    try:
        return json.loads(role_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ── Goal Decomposition ──────────────────────────────────────────


DECOMPOSITION_PROMPT = (
    "You are an Architect. Decompose the following goal into a sequence of "
    "atomic, independently executable subtasks. Each subtask should be small "
    "enough to be completed by a single specialist agent in one pass.\n\n"
    "Available roles: {roles}\n"
    "For each role, the capabilities are: {capabilities}\n\n"
    "Goal: {goal}\n\n"
    "Return your answer as a JSON array of objects. Each object must have:\n"
    "  - 'task': a clear, actionable description of the subtask\n"
    "  - 'role': one of the available role names\n"
    "  - 'dependencies': array of indices (0-based) of tasks that must complete first\n\n"
    "Example:\n"
    "[\n"
    "  {{\"task\": \"Create the module skeleton\", \"role\": \"developer\", \"dependencies\": []}},\n"
    "  {{\"task\": \"Write unit tests\", \"role\": \"qa\", \"dependencies\": [0]}}\n"
    "]\n\n"
    "Return ONLY the JSON array, nothing else."
)


def decompose_goal(goal: str) -> List[Dict[str, Any]]:
    """Break a high-level goal into ordered subtasks with role assignments.

    Uses the LLM to produce a structured decomposition. Falls back to a
    single-task list if the LLM is unavailable or returns invalid JSON.

    Returns:
        List of dicts with keys: task, role, dependencies
    """
    available_roles = list_roles()
    if not available_roles:
        available_roles = ["developer"]

    role_caps = {}
    for rname in available_roles:
        role_data = load_role(rname)
        if role_data:
            role_caps[rname] = ", ".join(role_data.get("capabilities", []))

    prompt = DECOMPOSITION_PROMPT.format(
        goal=goal,
        roles=", ".join(available_roles),
        capabilities=json.dumps(role_caps, indent=2),
    )

    result = query_llm(prompt, temperature=0.3)

    append_event(
        "tool:orchestrator",
        {
            "action": "decompose_goal",
            "goal": goal,
            "llm_success": result.get("success", False),
        },
    )

    if result["success"] and result.get("content"):
        try:
            tasks = json.loads(result["content"].strip())
            if isinstance(tasks, list) and all(
                isinstance(t, dict) and "task" in t and "role" in t for t in tasks
            ):
                return tasks
        except (json.JSONDecodeError, TypeError):
            pass

    return [{"task": goal, "role": "developer", "dependencies": []}]


# ── Task Delegation ─────────────────────────────────────────────


def delegate_task(
    task_description: str,
    role_name: str,
    context: Optional[str] = None,
    dependencies: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Create a registry task and execute it with the given agent persona.

    Loads the role's system prompt, appends the task description and any
    additional context, and sends the combined prompt to the LLM.

    Returns:
        Dict with keys: task_id, success, output, error
    """
    role = load_role(role_name)
    if not role:
        error = f"Unknown role: {role_name}"
        append_event(
            "tool:orchestrator",
            {"action": "delegate_task", "task": task_description, "success": False, "error": error},
        )
        return {"task_id": None, "success": False, "output": "", "error": error}

    task = create_new_task(
        description=task_description,
        agent=role_name,
    )

    task_id = task.get("id")
    registry_update(task_id, TASK_STATUS_IN_PROGRESS)

    system_prompt = role["system_prompt"]
    user_prompt = f"Task: {task_description}"
    if context:
        user_prompt += f"\n\nAdditional context:\n{context}"

    llm_result = query_llm(user_prompt, system_prompt=system_prompt)

    output = llm_result.get("content", "")
    success = llm_result.get("success", False)

    if success:
        registry_update(task_id, TASK_STATUS_DONE, notes=output[:500])
        store_insight(
            f"task-{task_id}-{role_name}",
            f"Task: {task_description}\nResult: {output[:1000]}",
        )
        append_event(
            "tool:orchestrator",
            {"action": "delegate_task", "task_id": task_id, "role": role_name, "success": True},
        )
        return {"task_id": task_id, "success": True, "output": output, "error": None}
    else:
        error_msg = llm_result.get("error", "Unknown LLM error")
        registry_update(task_id, TASK_STATUS_FAILED, notes=error_msg[:500])
        append_event(
            "tool:orchestrator",
            {
                "action": "delegate_task",
                "task_id": task_id,
                "role": role_name,
                "success": False,
                "error": error_msg,
            },
        )
        return {"task_id": task_id, "success": False, "output": "", "error": error_msg}


# ── Monitoring ──────────────────────────────────────────────────


def monitor_tasks() -> Dict[str, List[Dict[str, Any]]]:
    """Poll the task registry and group tasks by status.

    Returns:
        Dict mapping status string to list of task dicts.
    """
    all_tasks = list_tasks()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for t in all_tasks:
        status = t.get("status", "unknown")
        grouped.setdefault(status, []).append(t)

    append_event(
        "tool:orchestrator",
        {
            "action": "monitor_tasks",
            "counts": {k: len(v) for k, v in grouped.items()},
            "success": True,
        },
    )
    return grouped


def get_failed_tasks() -> List[Dict[str, Any]]:
    """Return all tasks currently in 'failed' status."""
    all_tasks = list_tasks()
    return [t for t in all_tasks if t.get("status") == TASK_STATUS_FAILED]


# ── Failure Handling ────────────────────────────────────────────


def handle_failure(
    task: Dict[str, Any],
    error: str,
    max_retries: int = MAX_RETRIES,
) -> Dict[str, Any]:
    """Decide and execute a recovery action for a failed task.

    Strategy:
      1. Retry with the same role (up to max_retries times).
      2. If retries exhausted, re-assign to a different role.
      3. If re-assignment also fails, escalate.

    Returns:
        Dict with keys: action, task_id, success, output, error
    """
    task_id = task["id"]
    description = task["description"]
    original_role = task.get("agent", "developer")
    retry_count = task.get("retry_count", 0)

    if retry_count < max_retries:
        task["retry_count"] = retry_count + 1
        registry_update(task_id, TASK_STATUS_PENDING, notes=f"Retry {retry_count + 1}/{max_retries}")
        result = delegate_task(description, original_role)
        action = "retry"

        append_event(
            "tool:orchestrator",
            {
                "action": "handle_failure",
                "task_id": task_id,
                "strategy": action,
                "retry_count": retry_count + 1,
                "success": result["success"],
            },
        )
        return {
            "action": action,
            "task_id": task_id,
            "success": result["success"],
            "output": result.get("output", ""),
            "error": result.get("error"),
        }

    alternative_roles = [r for r in list_roles() if r != original_role]
    if alternative_roles:
        new_role = alternative_roles[0]
        registry_update(task_id, TASK_STATUS_PENDING, notes=f"Re-assign to {new_role}")
        result = delegate_task(description, new_role)
        action = "re-assign"

        append_event(
            "tool:orchestrator",
            {
                "action": "handle_failure",
                "task_id": task_id,
                "strategy": action,
                "new_role": new_role,
                "success": result["success"],
            },
        )

        if not result["success"]:
            registry_update(task_id, TASK_STATUS_ESCALATED, notes=f"Escalated after re-assign to {new_role}")
            store_insight(
                f"escalation-task-{task_id}",
                f"Task: {description}\nOriginal role: {original_role}\n"
                f"Re-assigned to: {new_role}\nError: {error}",
            )
            return {
                "action": "escalate",
                "task_id": task_id,
                "success": False,
                "output": "",
                "error": f"Escalated: {error}",
            }

        return {
            "action": action,
            "task_id": task_id,
            "success": result["success"],
            "output": result.get("output", ""),
            "error": result.get("error"),
        }

    registry_update(task_id, TASK_STATUS_ESCALATED, notes=f"Escalated: no alternative roles")
    store_insight(
        f"escalation-task-{task_id}",
        f"Task: {description}\nRole: {original_role}\nError: {error}",
    )

    append_event(
        "tool:orchestrator",
        {
            "action": "handle_failure",
            "task_id": task_id,
            "strategy": "escalate",
            "success": False,
        },
    )
    return {
        "action": "escalate",
        "task_id": task_id,
        "success": False,
        "output": "",
        "error": f"Escalated: {error}",
    }


# ── Orchestration Loop ──────────────────────────────────────────


def run_orchestration(goal: str, max_retries: int = MAX_RETRIES) -> Dict[str, Any]:
    """Execute a full orchestration cycle for a high-level goal.

    Steps:
      1. Decompose the goal into subtasks.
      2. Execute each subtask in dependency order, delegating to the assigned role.
      3. On failure, attempt recovery via handle_failure.
      4. Collect and return aggregated results.

    Returns:
        Dict with keys: goal, subtasks, results, summary
    """
    append_event(
        "tool:orchestrator",
        {"action": "orchestration_start", "goal": goal},
    )

    subtasks = decompose_goal(goal)
    results: List[Dict[str, Any]] = []
    task_id_map: Dict[int, int] = {}

    for idx, st in enumerate(subtasks):
        task_desc = st["task"]
        role = st["role"]
        deps = st.get("dependencies", [])

        dep_ids = [task_id_map.get(d) for d in deps if d in task_id_map]
        context = f"Dependencies (task IDs): {dep_ids}" if dep_ids else None

        result = delegate_task(task_desc, role, context=context, dependencies=dep_ids)
        tid = result.get("task_id")
        if tid is not None:
            task_id_map[idx] = tid

        recovery_action = None
        if not result["success"] and tid is not None:
            task_obj = get_task_by_id(tid)
            if task_obj:
                recovery = handle_failure(task_obj, result.get("error", ""), max_retries)
                recovery_action = recovery["action"]
                result["success"] = recovery["success"]
                result["output"] = recovery.get("output", "")
                result["error"] = recovery.get("error")

        result_entry = {
            "index": idx,
            "task": task_desc,
            "role": role,
            "task_id": result.get("task_id"),
            "success": result["success"],
            "output": result.get("output", "")[:500],
            "error": result.get("error"),
        }
        if recovery_action is not None:
            result_entry["recovery_action"] = recovery_action
        results.append(result_entry)

    summary = {
        "total": len(results),
        "succeeded": sum(1 for r in results if r["success"]),
        "failed": sum(1 for r in results if not r["success"]),
    }

    store_insight(
        f"orchestration-{goal[:40]}",
        f"Goal: {goal}\nSubtasks: {summary['total']}\n"
        f"Succeeded: {summary['succeeded']}\nFailed: {summary['failed']}",
    )

    append_event(
        "tool:orchestrator",
        {"action": "orchestration_complete", "goal": goal, "summary": summary},
    )

    return {
        "goal": goal,
        "subtasks": subtasks,
        "results": results,
        "summary": summary,
    }
