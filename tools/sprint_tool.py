"""Sprint Tool — the scrum cadence over the story backlog.

A sprint is a timeboxed commitment of backlog stories:

  plan_sprint()      backlog → sprint proposal (dependency- and priority-aware,
                     optionally capped by story-point capacity)
  start_sprint()     the PO's approval — activates and executes the commitment
  review_sprint()    increment summary for PO acceptance
  accept_story()     PO accepts/rejects each story; rejections return to backlog
  complete_sprint()  velocity from accepted points + retrospective

Stories are *defined* by the project pipeline (BA/architect phases) and sit in
the durable backlog; only a sprint turns their planned_tasks into real registry
tasks and executes them through the gated pipeline (delegate_task). PO
acceptance is deliberately outside the Definition of Done: the gates decide
"done", only the human decides "delivered the intent".
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import bootstrap
from bootstrap import append_event, load_json, save_json

from tools.llm_tool import query_llm
from tools.story_tool import (
    get_story,
    list_backlog,
    update_story_fields,
    derive_story_status,
    add_task_to_story,
    _story_num,
)

DEFAULT_POINTS = 3  # capacity accounting for unestimated stories

SPRINT_STATUS_PLANNING = "planning"
SPRINT_STATUS_ACTIVE = "active"
SPRINT_STATUS_REVIEW = "review"
SPRINT_STATUS_COMPLETE = "complete"
SPRINT_STATUS_CANCELLED = "cancelled"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registry_path():
    # Resolved at call time so runtime/test state redirection is honored.
    return bootstrap.STATE_DIR / "sprint_registry.json"


def _load_registry() -> Dict[str, Any]:
    return load_json(_registry_path(), {"version": "1.0.0", "sprints": [], "next_id": 1})


def _save_registry(registry: Dict[str, Any]) -> None:
    save_json(_registry_path(), registry)


def get_sprint(sprint_id: Any) -> Optional[Dict[str, Any]]:
    lookup = f"SPRINT-{sprint_id}" if isinstance(sprint_id, int) else str(sprint_id)
    for sprint in _load_registry().get("sprints", []):
        if sprint["id"] == lookup:
            return sprint
    return None


def list_sprints(project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    sprints = _load_registry().get("sprints", [])
    if project_id is not None:
        sprints = [s for s in sprints if s.get("project_id") == project_id]
    return sprints


def _update_sprint(sprint_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    registry = _load_registry()
    for sprint in registry.get("sprints", []):
        if sprint["id"] == sprint_id:
            sprint.update(updates)
            sprint["updated_at"] = _now()
            _save_registry(registry)
            return sprint
    return None


def _story_points_or_default(story: Dict[str, Any]) -> int:
    points = story.get("story_points")
    return int(points) if points else DEFAULT_POINTS


# ── Planning ────────────────────────────────────────────────────


def _select_stories(
    backlog: List[Dict[str, Any]],
    capacity_points: Optional[int],
) -> List[Dict[str, Any]]:
    """Pick backlog stories for a sprint: dependency-first, then priority.

    A story is eligible when each of its dependencies is either already done
    or selected earlier in this same sprint. Selection stops when the point
    capacity is exhausted (None = unlimited).
    """
    selected: List[Dict[str, Any]] = []
    selected_ids: set = set()
    used_points = 0
    prio_rank = {"high": 0, "medium": 1, "low": 2}

    # Stable order: priority then numeric id; iterate until no more fit.
    candidates = sorted(
        backlog,
        key=lambda s: (prio_rank.get(s.get("priority", "medium"), 1), _story_num(s["id"])),
    )

    progressed = True
    while progressed:
        progressed = False
        for story in candidates:
            if story["id"] in selected_ids:
                continue
            deps = story.get("dependencies", [])
            deps_ok = all(
                dep in selected_ids or derive_story_status(dep) == "done"
                for dep in deps
            )
            if not deps_ok:
                continue
            points = _story_points_or_default(story)
            if capacity_points is not None and used_points + points > capacity_points:
                continue
            selected.append(story)
            selected_ids.add(story["id"])
            used_points += points
            progressed = True

    return selected


SPRINT_GOAL_PROMPT = (
    "You are a Scrum Master drafting a sprint goal.\n\n"
    "The sprint contains these user stories:\n{stories}\n\n"
    "Write ONE sentence (max 140 characters) stating what this sprint's "
    "increment should demonstrate. Return ONLY the sentence."
)


def _draft_goal(stories: List[Dict[str, Any]]) -> str:
    titles = "\n".join(f"- {s['title']}" for s in stories)
    result = query_llm(SPRINT_GOAL_PROMPT.format(stories=titles), temperature=0.3)
    if result.get("success") and result.get("content"):
        return result["content"].strip().splitlines()[0][:140]
    return f"Deliver {len(stories)} backlog stories: " + ", ".join(
        s["title"] for s in stories[:3]
    )[:100]


def plan_sprint(
    project_id: str,
    capacity_points: Optional[int] = None,
    goal: Optional[str] = None,
) -> Dict[str, Any]:
    """Propose a sprint from the project's backlog.

    Creates the sprint in 'planning' status and stamps sprint_id on the
    selected stories (removing them from the open backlog). The sprint runs
    nothing until start_sprint() — that call is the PO's approval. Use
    cancel_sprint() to discard the proposal and return stories to the backlog.
    """
    backlog = list_backlog(project_id)
    if not backlog:
        return {"success": False, "error": f"No backlog stories for project {project_id}"}

    stories = _select_stories(backlog, capacity_points)
    if not stories:
        return {"success": False, "error": "No eligible stories fit the capacity/dependencies"}

    registry = _load_registry()
    num = registry.get("next_id", 1)
    registry["next_id"] = num + 1
    sprint_id = f"SPRINT-{num}"

    committed_points = sum(_story_points_or_default(s) for s in stories)
    sprint = {
        "id": sprint_id,
        "project_id": project_id,
        "goal": goal or _draft_goal(stories),
        "status": SPRINT_STATUS_PLANNING,
        "story_ids": [s["id"] for s in stories],
        "capacity_points": capacity_points,
        "committed_points": committed_points,
        "velocity_points": None,
        "story_results": [],
        "retrospective": None,
        "created_at": _now(),
        "updated_at": _now(),
        "started_at": None,
        "review_at": None,
        "completed_at": None,
    }
    registry["sprints"].append(sprint)
    _save_registry(registry)

    for story in stories:
        update_story_fields(story["id"], {"sprint_id": sprint_id})

    append_event(
        "tool:sprint",
        {"action": "plan", "sprint_id": sprint_id, "project_id": project_id,
         "stories": len(stories), "points": committed_points, "goal": sprint["goal"]},
    )
    return {"success": True, "sprint": sprint}


def cancel_sprint(sprint_id: str) -> Dict[str, Any]:
    """Discard a planned sprint, returning its stories to the backlog."""
    sprint = get_sprint(sprint_id)
    if not sprint:
        return {"success": False, "error": f"Sprint {sprint_id} not found"}
    if sprint["status"] != SPRINT_STATUS_PLANNING:
        return {"success": False, "error": f"Only planning sprints can be cancelled (status: {sprint['status']})"}

    for sid in sprint.get("story_ids", []):
        update_story_fields(sid, {"sprint_id": None})
    _update_sprint(sprint["id"], {"status": SPRINT_STATUS_CANCELLED})
    append_event("tool:sprint", {"action": "cancel", "sprint_id": sprint["id"]})
    return {"success": True, "sprint_id": sprint["id"]}


# ── Execution ───────────────────────────────────────────────────


def _execute_story(story: Dict[str, Any], project_id: Optional[str]) -> Dict[str, Any]:
    """Materialize a story's planned tasks into registry tasks and run them.

    Tasks execute in their declared order; a task whose intra-story
    dependencies did not succeed is skipped (recorded as such) rather than
    run against a broken foundation.
    """
    from tools.orchestrator_tool import delegate_task
    from tools.registry_tool import create_new_task

    planned = story.get("planned_tasks") or []
    story_id = story["id"]
    task_results: List[Dict[str, Any]] = []
    index_to_task_id: Dict[int, int] = {}
    index_success: Dict[int, bool] = {}

    for idx, spec in enumerate(planned):
        description = spec.get("description", "")
        role = spec.get("role", "developer")
        dep_indices = [d for d in (spec.get("dependencies") or []) if isinstance(d, int)]

        if not all(index_success.get(d, False) for d in dep_indices):
            task_results.append({
                "index": idx, "task_id": None, "success": False,
                "skipped": True, "reason": "dependency did not succeed",
            })
            index_success[idx] = False
            continue

        task = create_new_task(
            description=description,
            agent=role,
            story_id=story_id,
            project_id=project_id,
        )
        task_id = task.get("id")
        if not task_id:
            task_results.append({
                "index": idx, "task_id": None, "success": False,
                "skipped": False, "reason": task.get("error", "task creation failed"),
            })
            index_success[idx] = False
            continue

        add_task_to_story(story_id, task_id)
        dep_task_ids = [index_to_task_id[d] for d in dep_indices if d in index_to_task_id]
        context = f"Project: {project_id}\nStory: {story_id} — {story.get('title', '')}"
        if story.get("acceptance_criteria"):
            context += "\nAcceptance criteria:\n" + "\n".join(
                f"- {c}" for c in story["acceptance_criteria"]
            )
        if dep_task_ids:
            context += f"\nDepends on completed tasks: {dep_task_ids}"
        if story.get("po_notes"):
            context += f"\nPO feedback from a previous rejection:\n{story['po_notes']}"

        result = delegate_task(
            description,
            role,
            context=context,
            existing_task_id=task_id,
            story_id=story_id,
            project_id=project_id,
        )
        index_to_task_id[idx] = task_id
        index_success[idx] = bool(result.get("success"))
        task_results.append({
            "index": idx,
            "task_id": task_id,
            "success": bool(result.get("success")),
            "blocked": bool(result.get("blocked")),
            "error": result.get("error"),
        })

    succeeded = sum(1 for r in task_results if r["success"])
    return {
        "story_id": story_id,
        "title": story.get("title", ""),
        "task_count": len(task_results),
        "succeeded": succeeded,
        "failed": len(task_results) - succeeded,
        "all_succeeded": succeeded == len(task_results) and len(task_results) > 0,
        "task_results": task_results,
    }


def start_sprint(sprint_id: str, execute: bool = True) -> Dict[str, Any]:
    """Activate a planned sprint (the PO's approval) and execute it.

    Stories run in dependency order; every task goes through the gated
    delegate_task pipeline (three-key signoff, retries, circuit breaker).
    A blocked or failed story does not abort the sprint — independent stories
    still run; dependent ones are skipped. Ends in 'review' status.
    """
    sprint = get_sprint(sprint_id)
    if not sprint:
        return {"success": False, "error": f"Sprint {sprint_id} not found"}
    if sprint["status"] != SPRINT_STATUS_PLANNING:
        return {"success": False, "error": f"Sprint is '{sprint['status']}', expected planning"}

    sprint = _update_sprint(sprint["id"], {
        "status": SPRINT_STATUS_ACTIVE, "started_at": _now(),
    })
    append_event(
        "tool:sprint",
        {"action": "start", "sprint_id": sprint["id"], "project_id": sprint.get("project_id"),
         "stories": len(sprint.get("story_ids", []))},
    )
    if not execute:
        return {"success": True, "sprint": sprint}
    return execute_sprint(sprint["id"])


def execute_sprint(sprint_id: str) -> Dict[str, Any]:
    """Run every story committed to an active sprint, then move to review."""
    sprint = get_sprint(sprint_id)
    if not sprint:
        return {"success": False, "error": f"Sprint {sprint_id} not found"}
    if sprint["status"] != SPRINT_STATUS_ACTIVE:
        return {"success": False, "error": f"Sprint is '{sprint['status']}', expected active"}

    project_id = sprint.get("project_id")
    ordered = _order_sprint_stories(sprint)

    results: List[Dict[str, Any]] = []
    completed_ids: set = set()
    for story in ordered:
        deps = story.get("dependencies", [])
        deps_ok = all(
            dep in completed_ids or derive_story_status(dep) == "done"
            for dep in deps
        )
        if not deps_ok:
            results.append({
                "story_id": story["id"], "title": story.get("title", ""),
                "task_count": 0, "succeeded": 0, "failed": 0,
                "all_succeeded": False, "skipped": True,
                "reason": "unmet story dependency",
            })
            continue

        append_event("tool:sprint", {"action": "story_start", "sprint_id": sprint["id"], "story_id": story["id"]})
        story_result = _execute_story(story, project_id)
        results.append(story_result)
        if story_result["all_succeeded"]:
            completed_ids.add(story["id"])
        append_event(
            "tool:sprint",
            {"action": "story_end", "sprint_id": sprint["id"], "story_id": story["id"],
             "succeeded": story_result["succeeded"], "failed": story_result["failed"]},
        )

    sprint = _update_sprint(sprint["id"], {
        "status": SPRINT_STATUS_REVIEW,
        "review_at": _now(),
        "story_results": results,
    })
    append_event(
        "tool:sprint",
        {"action": "execution_complete", "sprint_id": sprint["id"],
         "stories_done": sum(1 for r in results if r.get("all_succeeded")),
         "stories_total": len(results)},
    )
    return {"success": True, "sprint": sprint, "results": results}


def _order_sprint_stories(sprint: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Sprint stories in dependency order (deps first), then priority/id."""
    stories = [get_story(sid) for sid in sprint.get("story_ids", [])]
    stories = [s for s in stories if s]
    in_sprint = {s["id"] for s in stories}
    prio_rank = {"high": 0, "medium": 1, "low": 2}

    ordered: List[Dict[str, Any]] = []
    placed: set = set()
    candidates = sorted(
        stories,
        key=lambda s: (prio_rank.get(s.get("priority", "medium"), 1), _story_num(s["id"])),
    )
    progressed = True
    while progressed:
        progressed = False
        for story in candidates:
            if story["id"] in placed:
                continue
            deps_in_sprint = [d for d in story.get("dependencies", []) if d in in_sprint]
            if all(d in placed for d in deps_in_sprint):
                ordered.append(story)
                placed.add(story["id"])
                progressed = True
    # Circular leftovers appended so nothing is silently dropped.
    for story in candidates:
        if story["id"] not in placed:
            ordered.append(story)
    return ordered


# ── Review, acceptance, completion ──────────────────────────────


def review_sprint(sprint_id: str) -> Dict[str, Any]:
    """Increment summary for the PO: per-story outcome awaiting acceptance."""
    sprint = get_sprint(sprint_id)
    if not sprint:
        return {"success": False, "error": f"Sprint {sprint_id} not found"}

    stories = []
    for sid in sprint.get("story_ids", []):
        story = get_story(sid)
        if not story:
            continue
        stories.append({
            "story_id": sid,
            "title": story.get("title", ""),
            "points": story.get("story_points"),
            "derived_status": derive_story_status(sid),
            "po_acceptance": story.get("po_acceptance"),
            "po_notes": story.get("po_notes"),
            "acceptance_criteria": story.get("acceptance_criteria", []),
        })
    return {
        "success": True,
        "sprint_id": sprint["id"],
        "goal": sprint.get("goal"),
        "status": sprint.get("status"),
        "committed_points": sprint.get("committed_points"),
        "stories": stories,
        "story_results": sprint.get("story_results", []),
    }


def accept_story(story_id: str, accepted: bool, notes: str = "") -> Dict[str, Any]:
    """Record the PO's accept/reject decision on a delivered story.

    Rejection returns the story to the product backlog (sprint_id cleared)
    with the PO's notes attached for re-refinement; it will NOT count toward
    velocity. Acceptance is the only thing that does.
    """
    story = get_story(story_id)
    if not story:
        return {"success": False, "error": f"Story {story_id} not found"}

    decision = "accepted" if accepted else "rejected"
    updates = {"po_acceptance": decision, "po_notes": notes or None}
    if not accepted:
        updates["sprint_id"] = None  # back to the backlog
    updated = update_story_fields(story["id"], updates)

    append_event(
        "tool:story",
        {"action": "po_acceptance", "story_id": story["id"],
         "decision": decision, "notes": (notes or "")[:300]},
    )
    return {"success": True, "story": updated}


def complete_sprint(sprint_id: str, run_retro: bool = True) -> Dict[str, Any]:
    """Close a sprint in review: compute velocity and run the retrospective.

    Velocity counts only PO-accepted story points. Stories the PO has not yet
    decided on are left as-is (they simply don't count) — completing the
    sprint never fabricates acceptance.
    """
    sprint = get_sprint(sprint_id)
    if not sprint:
        return {"success": False, "error": f"Sprint {sprint_id} not found"}
    if sprint["status"] != SPRINT_STATUS_REVIEW:
        return {"success": False, "error": f"Sprint is '{sprint['status']}', expected review"}

    velocity = 0
    accepted = 0
    for sid in sprint.get("story_ids", []):
        story = get_story(sid)
        if story and story.get("po_acceptance") == "accepted":
            accepted += 1
            velocity += _story_points_or_default(story)

    sprint = _update_sprint(sprint["id"], {
        "status": SPRINT_STATUS_COMPLETE,
        "completed_at": _now(),
        "velocity_points": velocity,
    })
    append_event(
        "tool:sprint",
        {"action": "complete", "sprint_id": sprint["id"],
         "velocity_points": velocity, "stories_accepted": accepted},
    )

    retro = None
    if run_retro:
        retro = run_retrospective(sprint["id"])

    return {"success": True, "sprint": sprint, "velocity_points": velocity, "retrospective": retro}


def get_velocity(project_id: str, window: int = 3) -> Dict[str, Any]:
    """Rolling velocity: mean accepted points over the last N completed sprints."""
    completed = [
        s for s in list_sprints(project_id)
        if s.get("status") == SPRINT_STATUS_COMPLETE and s.get("velocity_points") is not None
    ]
    recent = completed[-window:]
    values = [s["velocity_points"] for s in recent]
    return {
        "project_id": project_id,
        "sprints_counted": len(values),
        "velocity": (sum(values) / len(values)) if values else None,
        "history": [{"sprint_id": s["id"], "points": s["velocity_points"]} for s in recent],
    }


# ── Retrospective ───────────────────────────────────────────────

RETRO_PROMPT = (
    "You are running a scrum retrospective for an autonomous AI development "
    "team. Below are metrics mined from the sprint's event log.\n\n"
    "Sprint goal: {goal}\n"
    "Stories committed: {committed} | fully done: {done}\n"
    "Metrics:\n{stats}\n\n"
    "Propose AT MOST 3 concrete process changes, each targeting exactly one "
    "lever: an agent role prompt, a quality-gate definition, a retry/routing "
    "rule, or a Definition of Ready/Done criterion. Each proposal must name "
    "the metric it expects to improve.\n\n"
    "Return ONLY a JSON array of objects with keys: "
    '"area", "proposal", "expected_effect". No other text.'
)


def _collect_sprint_stats(sprint: Dict[str, Any]) -> Dict[str, int]:
    """Mine the event log for this sprint's window and aggregate signals."""
    stats = {
        "qa_gate_failures": 0,
        "architect_gate_failures": 0,
        "contract_gate_failures": 0,
        "task_retries": 0,
        "tasks_blocked_for_human": 0,
        "llm_failures": 0,
        "events_scanned": 0,
    }
    started = sprint.get("started_at") or sprint.get("created_at") or ""
    ended = sprint.get("completed_at") or _now()

    log_path = bootstrap.EVENT_LOG_PATH
    if not log_path.exists():
        return stats

    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = event.get("timestamp", "")
            if not (started <= ts <= ended):
                continue
            stats["events_scanned"] += 1
            action = event.get("action", "")
            etype = event.get("type", "")
            if action == "qa_fail":
                stats["qa_gate_failures"] += 1
            elif action == "architect_fail":
                stats["architect_gate_failures"] += 1
            elif action == "contract_fail":
                stats["contract_gate_failures"] += 1
            elif action == "delegate_task_retry":
                stats["task_retries"] += 1
            elif action == "blocked_needs_human":
                stats["tasks_blocked_for_human"] += 1
            elif etype == "tool:llm_reasoning" and event.get("success") is False:
                stats["llm_failures"] += 1
    return stats


def run_retrospective(sprint_id: str) -> Dict[str, Any]:
    """Inspect-and-adapt: mine the sprint's event-log slice, propose up to 3
    process changes, and store them on the sprint and in the knowledge base.

    The proposals are recorded for the PO to approve — they are not
    self-applied. This is the team's improvement loop, not an autonomous
    rewrite of its own rules.
    """
    sprint = get_sprint(sprint_id)
    if not sprint:
        return {"success": False, "error": f"Sprint {sprint_id} not found"}

    stats = _collect_sprint_stats(sprint)
    done = sum(1 for r in sprint.get("story_results", []) if r.get("all_succeeded"))

    proposals: List[Dict[str, Any]] = []
    result = query_llm(
        RETRO_PROMPT.format(
            goal=sprint.get("goal", ""),
            committed=len(sprint.get("story_ids", [])),
            done=done,
            stats=json.dumps(stats, indent=2),
        ),
        temperature=0.3,
    )
    if result.get("success") and result.get("content"):
        match = re.search(r"\[.*\]", result["content"], re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, list):
                    proposals = [
                        {
                            "area": str(p.get("area", ""))[:120],
                            "proposal": str(p.get("proposal", ""))[:600],
                            "expected_effect": str(p.get("expected_effect", ""))[:300],
                            "status": "proposed",
                        }
                        for p in parsed[:3]
                        if isinstance(p, dict) and p.get("proposal")
                    ]
            except json.JSONDecodeError:
                proposals = []

    retro = {
        "stats": stats,
        "proposals": proposals,
        "generated_at": _now(),
    }
    _update_sprint(sprint["id"], {"retrospective": retro})
    append_event(
        "tool:sprint",
        {"action": "retrospective", "sprint_id": sprint["id"],
         "proposals": len(proposals), "stats": stats},
    )

    try:
        from tools.knowledge_tool import store_insight
        if proposals:
            store_insight(
                f"retro-{sprint['id']}",
                f"Sprint {sprint['id']} retrospective.\nStats: {json.dumps(stats)}\n"
                + "\n".join(f"- [{p['area']}] {p['proposal']}" for p in proposals),
            )
    except Exception:
        pass

    return {"success": True, "sprint_id": sprint["id"], **retro}
