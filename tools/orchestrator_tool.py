"""Orchestrator Tool — multi-agent task decomposition, delegation, and monitoring.

Provides the core logic for breaking high-level goals into atomic subtasks,
assigning them to specialist agent personas, executing them via the LLM,
and monitoring for failures with automatic retry/re-assign/escalate recovery.
"""

import json
import yaml
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
from tools.story_tool import (
    create_story,
    get_story_execution_order,
    get_ready_stories,
    derive_story_status,
    get_story,
)

ROLES_DIR = BASE_DIR / "roles"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_PENDING_VERIFICATION = "pending_verification"
TASK_STATUS_TESTING = "testing"
TASK_STATUS_TESTING_PASSED = "testing_passed"
TASK_STATUS_ARCHITECT_REVIEW = "architect_review"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_REJECTED = "rejected"
TASK_STATUS_ESCALATED = "escalated"
TASK_STATUS_BLOCKED_INTERFACE_GAP = "blocked_interface_gap"

MAX_RETRIES = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Role Management ─────────────────────────────────────────────


def list_roles() -> List[str]:
    """Return the names of all available agent personas."""
    if not ROLES_DIR.is_dir():
        return []
    return [f.stem for f in sorted(ROLES_DIR.glob("*.yaml"))]


def load_role(role_name: str) -> Optional[Dict[str, Any]]:
    """Load an agent role definition by name.

    Args:
        role_name: The name of the role (e.g., 'architect', 'developer').

    Returns:
        The role dictionary, or None if not found or invalid.
    """
    role_path = ROLES_DIR / f"{role_name}.yaml"
    if not role_path.is_file():
        return None
    try:
        return yaml.safe_load(role_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None


# ── Goal Decomposition ──────────────────────────────────────────


DECOMPOSITION_PROMPT = (
    "You are an Architect. Decompose the following goal into a sequence of "
    "atomic, independently executable subtasks. Each subtask should be small "
    "enough to be completed by a single specialist agent in one pass.\n\n"
    "CRITICAL: Return ONLY a JSON array of objects. Do NOT provide any "
    "architecture descriptions, introductory text, or explanations. "
    "The output must be parsable as a JSON list.\n\n"
    "Available roles: {roles}\n"
    "For each role, the capabilities are: {capabilities}\n\n"
    "Goal: {goal}\n\n"
    "Example Output Format:\n"
    "[\n"
    "  {{\"task\": \"Create module skeleton\", \"role\": \"developer\", \"dependencies\": []}},\n"
    "  {{\"task\": \"Write unit tests\", \"role\": \"qa\", \"dependencies\": [0]}}\n"
    "]\n"
)


def decompose_goal(goal: str) -> List[Dict[str, Any]]:
    """Break a high-level goal into ordered subtasks with role assignments.

    Uses the LLM to produce a structured decomposition. Falls back to a
    single-task list if the LLM is unavailable or returns invalid JSON.

    After successful decomposition, generates an interface specification
    that serves as the contract for all tasks.

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

    tasks: List[Dict[str, Any]] = []
    if result["success"] and result.get("content"):
        try:
            parsed = json.loads(result["content"].strip())
            if isinstance(parsed, list):
                # Verify it's a valid task list
                if all(isinstance(t, dict) and "task" in t and "role" in t for t in parsed):
                    tasks = parsed
            elif isinstance(parsed, dict) and "architecture" in parsed:
                # RECOVERY: The LLM provided architecture instead of tasks.
                # Attempt to turn that architecture into a task list via a recovery call.
                recovery_prompt = (
                    "You are an Architect. You just designed a system architecture. "
                    "Now, decompose that architecture into a JSON array of atomic subtasks. "
                    "Each object must have 'task', 'role', and 'dependencies'.\n\n"
                    f"Architecture to decompose:\n{parsed['architecture']}\n\n"
                    "Return ONLY the JSON array."
                )
                recovery_result = query_llm(recovery_prompt, temperature=0.1)
                if recovery_result["success"]:
                    recovered_tasks = json.loads(recovery_result["content"].strip())
                    if isinstance(recovered_tasks, list):
                        tasks = recovered_tasks
        except (json.JSONDecodeError, TypeError):
            pass

    if not tasks:
        return [{"task": goal, "role": "developer", "dependencies": []}]

    # Generate interface specification from decomposition
    try:
        from tools.interface_tool import generate_interface_spec
        spec_id = generate_interface_spec(goal, tasks)
        if spec_id:
            for t in tasks:
                t["interface_spec_id"] = spec_id
            append_event(
                "tool:orchestrator",
                {"action": "decompose_goal_spec_generated", "goal": goal, "spec_id": spec_id},
            )
    except ImportError:
        pass

    return tasks


# ── Task Delegation ─────────────────────────────────────────────


def delegate_task(
    task_description: str,
    role_name: str,
    context: Optional[str] = None,
    dependencies: Optional[List[int]] = None,
    run_stage_gates: bool = True,
    story_id: Optional[str] = None,
    interface_spec_id: Optional[str] = None,
    existing_task_id: Optional[int] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a registry task and execute it with the given agent persona.

    Loads the role's system prompt, appends the task description and any
    additional context, and sends the combined prompt to the LLM.

    If run_stage_gates is True, routes successful tasks through the
    QA and Architect verification pipeline.

    If story_id is provided, links the task to that story.

    If interface_spec_id is provided, injects the interface contract into
    the task context.

    If existing_task_id is provided, reuses that task instead of creating a new one.

    Returns:
        Dict with keys: task_id, success, output, error, stage_gates
    """
    role = load_role(role_name)
    if not role:
        error = f"Unknown role: {role_name}"
        append_event(
            "tool:orchestrator",
            {"action": "delegate_task", "task": task_description, "success": False, "error": error},
        )
        return {"task_id": None, "success": False, "output": "", "error": error, "stage_gates": []}

    if existing_task_id is not None:
        task_id = existing_task_id
    else:
        task = create_new_task(
            description=task_description,
            agent=role_name,
            story_id=story_id,
            interface_spec_id=interface_spec_id,
            project_id=project_id,
        )
        task_id = task.get("id")

        # Link to story if provided
        if story_id and task_id:
            try:
                from tools.story_tool import add_task_to_story
                add_task_to_story(story_id, task_id)
            except ImportError:
                pass

    registry_update(task_id, TASK_STATUS_IN_PROGRESS)

    system_prompt = role["system_prompt"]
    user_prompt = f"Task: {task_description}"
    if context:
        user_prompt += f"\n\nAdditional context:\n{context}"

    # Inject interface specification contract
    spec_id = interface_spec_id
    spec_content = None
    if not spec_id:
        try:
            from tools.interface_tool import get_task_spec_id, get_active_spec
            spec_id = get_task_spec_id(task_id) or get_active_spec()
        except ImportError:
            pass

    if spec_id:
        try:
            from tools.interface_tool import load_interface_spec
            spec_content = load_interface_spec(spec_id)
            if spec_content:
                user_prompt += (
                    f"\n\nINTERFACE CONTRACT:\n"
                    f"The following specification defines the contract your implementation must satisfy:\n"
                    f"```\n{spec_content}\n```\n\n"
                    f"IMPORTANT: You MUST implement code that satisfies this contract. "
                    f"Do NOT deviate from the defined interfaces. "
                    f"If you identify a need for an additional property or endpoint "
                    f"(an Interface Gap), mark the task as blocked and report the gap."
                )
        except ImportError:
            pass

    error_msg = "Unknown error"

    # Route developer tasks through OpenCode; other roles use direct LLM
    if role_name == "developer":
        try:
            from tools.opencode_tool import execute_task, start_server, is_server_running

            if not is_server_running():
                srv = start_server()
                if not srv.get("success"):
                    error_msg = srv.get("error", "OpenCode server failed to start")
                    append_event(
                        "tool:orchestrator",
                        {"action": "delegate_task", "task_id": task_id, "role": role_name,
                         "success": False, "error": error_msg},
                    )
                    return {"task_id": task_id, "success": False, "output": "",
                            "error": error_msg, "stage_gates": []}

            oc_result = execute_task(
                task_description=task_description,
                system_prompt=system_prompt,
                interface_spec=spec_content,
                additional_context=context,
            )
            output = oc_result.get("output", "")
            success = oc_result.get("success", False)
            if not success:
                error_msg = oc_result.get("error", "OpenCode task failed")
        except ImportError:
            append_event(
                "tool:orchestrator",
                {"action": "delegate_task", "task_id": task_id, "role": role_name,
                 "success": False, "error": "opencode_tool not available, falling back to LLM"},
            )
            llm_result = query_llm(user_prompt, system_prompt=system_prompt)
            output = llm_result.get("content", "")
            success = llm_result.get("success", False)
            if not success:
                error_msg = llm_result.get("error", "Unknown LLM error")
    else:
        llm_result = query_llm(user_prompt, system_prompt=system_prompt)
        output = llm_result.get("content", "")
        success = llm_result.get("success", False)
        if not success:
            error_msg = llm_result.get("error", "Unknown LLM error")

    if success:
        # Run stage gate pipeline
        stage_gate_results = []
        if run_stage_gates:
            try:
                from tools.stage_gate_tool import run_full_pipeline
                pipeline = run_full_pipeline(task_id, developer_notes=output[:500])
                stage_gate_results = pipeline.get("results", [])
                success = pipeline["success"]
                if not success:
                    stopped = pipeline.get("stopped_at", "unknown")
                    last_failure = next(
                        (r for r in stage_gate_results if not r.get("passed", True)),
                        {},
                    )
                    error_msg = last_failure.get("feedback", f"Rejected at {stopped} gate")
                    append_event(
                        "tool:orchestrator",
                        {
                            "action": "delegate_task",
                            "task_id": task_id,
                            "role": role_name,
                            "success": False,
                            "rejected_at": stopped,
                            "error": error_msg[:500],
                        },
                    )
                    return {
                        "task_id": task_id,
                        "success": False,
                        "output": output,
                        "error": error_msg,
                        "stage_gates": stage_gate_results,
                    }
            except ImportError:
                pass

        registry_update(task_id, TASK_STATUS_DONE, notes=output[:500])
        store_insight(
            f"task-{task_id}-{role_name}",
            f"Task: {task_description}\nResult: {output[:1000]}",
        )
        append_event(
            "tool:orchestrator",
            {"action": "delegate_task", "task_id": task_id, "role": role_name, "success": True},
        )
        return {"task_id": task_id, "success": True, "output": output, "error": None, "stage_gates": stage_gate_results}
    else:
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
        return {"task_id": task_id, "success": False, "output": "", "error": error_msg, "stage_gates": []}


# ── Interface Gap Handling ──────────────────────────────────────


def report_interface_gap(
    task_id: int,
    gap_description: str,
) -> Dict[str, Any]:
    """Handle an interface gap reported by a developer agent.

    When a developer identifies that the interface specification is missing
    an endpoint, property, or contract needed for their task, this function:
      1. Marks the task as blocked_interface_gap
      2. Attempts to update the spec via the orchestrator
      3. Logs the gap for human review if auto-update is not possible

    Returns:
        Dict with keys: success, action_taken, spec_updated
    """
    task = get_task_by_id(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}

    spec_id = task.get("interface_spec_id")
    if not spec_id:
        try:
            from tools.interface_tool import get_active_spec
            spec_id = get_active_spec()
        except ImportError:
            pass

    if not spec_id:
        registry_update(
            task_id,
            TASK_STATUS_BLOCKED_INTERFACE_GAP,
            notes=f"[INTERFACE GAP] No active spec. Gap: {gap_description[:500]}",
        )
        store_insight(
            f"interface-gap-task-{task_id}",
            f"Task: {task['description']}\nGap: {gap_description}",
        )
        append_event(
            "tool:orchestrator",
            {
                "action": "interface_gap",
                "task_id": task_id,
                "success": False,
                "error": "no active spec",
                "gap": gap_description[:500],
            },
        )
        return {"success": False, "action_taken": "blocked", "spec_updated": False}

    # Attempt to update the spec
    spec_updated = False
    try:
        from tools.interface_tool import update_interface_spec
        spec_updated = update_interface_spec(spec_id, gap_description, task["description"])
    except ImportError:
        pass

    if spec_updated:
        registry_update(
            task_id,
            TASK_STATUS_PENDING,
            notes=f"[INTERFACE GAP RESOLVED] Spec updated. Ready for re-execution.",
        )
        append_event(
            "tool:orchestrator",
            {
                "action": "interface_gap_resolved",
                "task_id": task_id,
                "spec_id": spec_id,
                "gap": gap_description[:500],
            },
        )
        return {"success": True, "action_taken": "spec_updated", "spec_updated": True}
    else:
        registry_update(
            task_id,
            TASK_STATUS_BLOCKED_INTERFACE_GAP,
            notes=f"[INTERFACE GAP] Could not auto-update spec. Gap: {gap_description[:500]}",
        )
        store_insight(
            f"interface-gap-task-{task_id}",
            f"Task: {task['description']}\nSpec: {spec_id}\nGap: {gap_description}",
        )
        append_event(
            "tool:orchestrator",
            {
                "action": "interface_gap_blocked",
                "task_id": task_id,
                "spec_id": spec_id,
                "gap": gap_description[:500],
            },
        )
        return {"success": False, "action_taken": "blocked", "spec_updated": False}


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
        result = delegate_task(description, original_role, existing_task_id=task_id)
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
        result = delegate_task(description, new_role, existing_task_id=task_id)
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


def run_orchestration(
    goal: str,
    max_retries: int = MAX_RETRIES,
    story_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a full orchestration cycle for a high-level goal.

    Steps:
      1. Decompose the goal into subtasks.
      2. Execute each subtask in dependency order, delegating to the assigned role.
      3. On failure, attempt recovery via handle_failure.
      4. Collect and return aggregated results.
      5. If story_id provided, attempt story completion when all tasks succeed.

    Returns:
        Dict with keys: goal, subtasks, results, summary
    """
    append_event(
        "tool:orchestrator",
        {"action": "orchestration_start", "goal": goal, "story_id": story_id},
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
        spec_id = st.get("interface_spec_id")

        result = delegate_task(
            task_desc, role, context=context, dependencies=dep_ids,
            story_id=story_id, interface_spec_id=spec_id,
        )
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

    # Attempt story completion if all tasks succeeded and story_id was provided
    story_completion = None
    if story_id and summary["failed"] == 0:
        try:
            from tools.story_tool import complete_story
            story_completion = complete_story(story_id)
        except ImportError:
            pass

    return {
        "goal": goal,
        "subtasks": subtasks,
        "results": results,
        "summary": summary,
        "story_id": story_id,
        "story_completion": story_completion,
    }


# ── Multi-Story Orchestration ───────────────────────────────────


def run_story_orchestration(
    goals: List[Dict[str, Any]],
    max_retries: int = MAX_RETRIES,
) -> Dict[str, Any]:
    """Execute multiple stories in dependency order.

    Each entry in goals is a dict with:
      - 'goal': the high-level goal string
      - 'title': story title
      - 'description': story description (optional)
      - 'acceptance_criteria': list of criteria (optional)
      - 'priority': high/medium/low (optional)
      - 'dependencies': list of indices into goals[] that must complete first

    Stories are created with dependency tracking, then executed in
    topological order. A story only runs once all its dependencies
    have reached 'done' status.

    Returns:
        Dict with keys: stories, results, summary
    """
    append_event(
        "tool:orchestrator",
        {"action": "story_orchestration_start", "goal_count": len(goals)},
    )

    story_id_map: Dict[int, str] = {}
    dep_map: Dict[int, List[str]] = {}

    for idx, g in enumerate(goals):
        deps = g.get("dependencies", [])
        dep_map[idx] = [story_id_map[d] for d in deps if d in story_id_map]

        story = create_story(
            title=g["title"],
            description=g.get("description", ""),
            acceptance_criteria=g.get("acceptance_criteria"),
            priority=g.get("priority", "medium"),
            dependencies=dep_map[idx],
        )
        story_id_map[idx] = story["id"]

    execution_order = get_story_execution_order()
    results: List[Dict[str, Any]] = []
    story_results: Dict[str, Dict[str, Any]] = {}

    for story_id in execution_order:
        idx = list(story_id_map.values()).index(story_id) if story_id in story_id_map.values() else -1
        if idx < 0:
            continue

        goal_entry = goals[idx]
        story = get_story(story_id)
        if not story:
            continue

        deps = story.get("dependencies", [])
        blocked = False
        for dep_id in deps:
            dep_status = derive_story_status(dep_id)
            if dep_status == "failed":
                append_event(
                    "tool:orchestrator",
                    {"action": "story_blocked", "story_id": story_id, "blocked_by": dep_id,
                     "reason": "dependency failed"},
                )
                results.append({
                    "story_id": story_id,
                    "goal": goal_entry["goal"],
                    "success": False,
                    "error": f"Blocked: dependency {dep_id} failed",
                    "status": "blocked",
                })
                blocked = True
                break
            if dep_status not in ("done",):
                append_event(
                    "tool:orchestrator",
                    {"action": "story_blocked", "story_id": story_id, "blocked_by": dep_id,
                     "reason": f"dependency status: {dep_status}"},
                )
                results.append({
                    "story_id": story_id,
                    "goal": goal_entry["goal"],
                    "success": False,
                    "error": f"Blocked: dependency {dep_id} not done ({dep_status})",
                    "status": "blocked",
                })
                blocked = True
                break

        if blocked:
            continue

        result = run_orchestration(
            goal_entry["goal"],
            max_retries=max_retries,
            story_id=story_id,
        )
        story_results[story_id] = result
        results.append({
            "story_id": story_id,
            "goal": goal_entry["goal"],
            "success": result["summary"]["failed"] == 0,
            "summary": result["summary"],
            "status": derive_story_status(story_id),
        })

    summary = {
        "total": len(results),
        "succeeded": sum(1 for r in results if r["success"]),
        "failed": sum(1 for r in results if not r["success"]),
        "blocked": sum(1 for r in results if r.get("status") == "blocked"),
    }

    append_event(
        "tool:orchestrator",
        {"action": "story_orchestration_complete", "summary": summary},
    )

    return {
        "stories": [story_id_map[i] for i in range(len(goals))],
        "results": results,
        "summary": summary,
    }
