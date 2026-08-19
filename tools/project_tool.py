"""Project Tool — full SDLC pipeline entry point.

Accepts a high-level project description and orchestrates the *definition*
side of the lifecycle:
  1. BA Phase: Analyze requirements, detect ambiguities, create clarification requests
  2. Architect Phase: Review refined requirements, produce architecture and interface specs
  3. Backlog Phase: Persist estimated, project-scoped stories (with deferred
     task specs) to the durable product backlog and propose a sprint.

Execution is deliberately separate: sprints (tools/sprint_tool.py) pull
stories from the backlog and run them through the gated pipeline. Submitting
a project therefore ends with a reviewable backlog + sprint proposal, not a
finished build — unless auto_execute is set, which starts the first sprint
immediately.

Follow-up work enters through submit_change_request(), which gives the BA the
existing requirements, architecture, and story ledger as context and appends
new stories to the backlog instead of re-running the pipeline from scratch.

The pipeline pauses at clarification points and can be resumed once all
ambiguities are resolved (via Telegram or Dashboard).
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from bootstrap import (
    BASE_DIR,
    append_event,
    ensure_initialized,
    load_json,
    load_project_state,
    save_json,
    save_project_state,
)
from tools.llm_tool import query_llm

def _extract_json(content: str) -> Optional[Dict[str, Any]]:
    """Robustly extract JSON from a string that may contain markdown fences or preamble."""
    match = re.search(r'([\[\{].*[\]\}])', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            return None
    return None

# --- Configuration ----------------------------------------------
MOCK_EXECUTION = False

USER_PROJECTS_ROOT = Path("~/dev-projects/sdlc-ai/user_projects").expanduser()

PHASE_BA_ANALYSIS = "ba_analysis"
PHASE_ARCHITECT_DESIGN = "architect_design"
PHASE_EXECUTION = "execution"
PHASE_BACKLOG_READY = "backlog_ready"
PHASE_COMPLETE = "complete"

PROJECT_STATUS_ACTIVE = "active"
PROJECT_STATUS_AWAITING_CLARIFICATION = "awaiting_clarification"
PROJECT_STATUS_COMPLETED = "completed"
PROJECT_STATUS_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _broadcast_pipeline_status(project_id: str, phase: str, message: str) -> None:
    """Emit a pipeline status event the frontend can pick up."""
    append_event(
        "tool:project",
        {
            "action": "pipeline_status",
            "project_id": project_id,
            "phase": phase,
            "message": message,
        },
    )


# --- Storage Helpers ---------------------------------------------

def _ensure_projects_root() -> Path:
    """Ensure the projects root exists."""
    USER_PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    return USER_PROJECTS_ROOT


def _project_path(project_id: str) -> Path:
    """Resolve the path to a project's state.json."""
    return USER_PROJECTS_ROOT / project_id / "state.json"


def _load_project(project_id: str) -> Optional[Dict[str, Any]]:
    """Load project data from its isolated subfolder."""
    # Try direct path first (for projects created with project_id as dir name)
    path = _project_path(project_id)
    if path.exists() and str(path) != "/dev/null":
        return load_json(path, None)
    # Search through all project directories for matching id
    root = _ensure_projects_root()
    for folder in root.iterdir():
        if folder.is_dir():
            state_file = folder / "state.json"
            if state_file.exists():
                data = load_json(state_file, None)
                if data and data.get("id") == project_id:
                    return data
    return None


def _save_project(project: Dict[str, Any]) -> None:
    """Save project data to its isolated subfolder."""
    folder_name = project.get("folder_name")
    if folder_name:
        path = USER_PROJECTS_ROOT / folder_name / "state.json"
    else:
        path = _project_path(project["id"])
    if str(path) != "/dev/null":
        save_json(path, project)


def list_projects() -> List[Dict[str, Any]]:
    """Return all project summaries."""
    root = _ensure_projects_root()
    projects = []
    for folder in sorted(root.iterdir()):
        if folder.is_dir():
            state_file = folder / "state.json"
            if state_file.exists():
                data = load_json(state_file, None)
                if data:
                    projects.append({
                        "id": data["id"],
                        "name": data.get("name", ""),
                        "description": data.get("description", "")[:100],
                        "phase": data.get("phase", ""),
                        "status": data.get("status", ""),
                        "created_at": data.get("created_at", ""),
                    })
    return projects


# ── BA Analysis Prompts ─────────────────────────────────────────

BA_ANALYSIS_PROMPT = """You are a Business Analyst. Analyze the following project description thoroughly.

Project Description:
{description}

Your job is to:
1. Identify any ambiguities, missing requirements, or unclear specifications that need human clarification.
2. Produce a refined, structured requirements document that captures everything that IS clear.

For EACH ambiguity you find, provide:
- question: A clear, specific question the human can answer.
- context: The surrounding context explaining why this matters.
- reason: Why this clarification is needed before proceeding.

Be thorough. Missing requirements discovered late are expensive.

Return your response as a JSON object with exactly two keys:
- "refined_requirements": A detailed markdown document of the clarified requirements.
- "ambiguities": An array of objects with keys "question", "context", "reason".
  If there are NO ambiguities, set this to an empty array [].

Return ONLY valid JSON, nothing else."""


def _ba_analyze(refined_requirements: str) -> Dict[str, Any]:
    """Run the BA analysis phase.
    
    Returns:
        Dict with keys: refined_requirements, ambiguities (list), success (bool)
    """
    prompt = BA_ANALYSIS_PROMPT.format(description=refined_requirements)
    result = query_llm(prompt, temperature=0.3)

    if not result["success"] or not result.get("content"):
        append_event(
            "tool:project",
            {"action": "ba_analyze", "success": False, "error": result.get("error")},
        )
        return {
            "success": False,
            "refined_requirements": "",
            "ambiguities": [],
            "error": result.get("error", "LLM query failed"),
        }

    content = result["content"].strip()
    parsed = _extract_json(content)

    if parsed is None:
        append_event(
            "tool:project",
            {"action": "ba_analyze", "success": False, "error": "Failed to parse JSON from LLM response"},
        )
        return {
            "success": True,
            "refined_requirements": content,
            "ambiguities": [],
        }

    refined = parsed.get("refined_requirements", content)
    ambiguities = parsed.get("ambiguities", [])
    append_event(
        "tool:project",
        {
            "action": "ba_analyze",
            "success": True,
            "ambiguity_count": len(ambiguities),
        },
    )
    return {
        "success": True,
        "refined_requirements": refined,
        "ambiguities": ambiguities,
    }


# ── Architect Design Prompts ────────────────────────────────────

ARCHITECT_DESIGN_PROMPT = """You are a Solution Architect. Based on the following refined requirements,
design the system architecture.

Requirements:
{requirements}

Produce:
1. A high-level architecture description covering:
   - System components and their responsibilities
   - Technology stack recommendations
   - Data models and relationships
   - Component interactions and data flow
2. An interface specification (OpenAPI 3.0 YAML or component interface spec)
   defining contracts between components.

Return your response as a JSON object with two keys:
- "architecture": A markdown document describing the system architecture.
- "interface_spec": A YAML string of the interface specification.

Return ONLY valid JSON, nothing else."""


def _architect_design(refined_requirements: str) -> Dict[str, Any]:
    """Run the Architect design phase.

    Returns:
        Dict with keys: architecture (str), interface_spec (str), success (bool)
    """
    prompt = ARCHITECT_DESIGN_PROMPT.format(requirements=refined_requirements)
    result = query_llm(prompt, temperature=0.3)

    if not result["success"] or not result.get("content"):
        append_event(
            "tool:project",
            {"action": "architect_design", "success": False, "error": result.get("error")},
        )
        return {
            "success": False,
            "architecture": "",
            "interface_spec": "",
            "error": result.get("error", "LLM query failed"),
        }

    content = result["content"].strip()
    parsed = _extract_json(content)

    if parsed is None:
        append_event(
            "tool:project",
            {"action": "architect_design", "success": False, "error": "Failed to parse JSON from LLM response"},
        )
        return {"success": False, "architecture": "", "interface_spec": "", "error": "JSON parse failed"}

    architecture = parsed.get("architecture", "")
    interface_spec = parsed.get("interface_spec", "")
    append_event(
        "tool:project",
        {"action": "architect_design", "success": True},
    )
    return {"success": True, "architecture": architecture, "interface_spec": interface_spec}


# ── Story/Task Creation from Architecture ───────────────────────

STORY_CREATION_PROMPT = """You are a Business Analyst. Based on the following architecture and requirements,
break the work into user stories and tasks.

Architecture:
{architecture}

Requirements:
{requirements}

Interface Specification:
{interface_spec}

CRITICAL: Every single user story MUST include a non-empty "tasks" array. Do not create a story without at least one task.

Create a list of user stories. Each story must have exactly these keys:
- title: A short, descriptive user story title.
- description: Detailed context and value statement (e.g., 'As a [user], I want to [action] so that [value]').
- acceptance_criteria: An array of specific, measurable criteria for completion.
- priority: One of: "high", "medium", or "low".
- story_points: An integer estimate of relative effort/complexity. Must be one of: 1, 2, 3, 5, 8. A story you would estimate above 8 must be split into smaller stories instead.
- tasks: An array of task objects. Each task must have:
    - description: A clear, actionable technical instruction.
    - role: One of: "developer", "qa", "researcher", "architect".
    - dependencies: An array of integer indices (0-based) representing other tasks in THIS story that MUST be completed first. If no dependencies, use [].

Return your response as a JSON array of story objects.
Return ONLY valid JSON, nothing else."""


def _create_stories_and_tasks(
    architecture: str,
    refined_requirements: str,
    interface_spec: str,
) -> List[Dict[str, Any]]:
    """Use LLM to create stories and tasks from the architecture."""
    prompt = STORY_CREATION_PROMPT.format(
        architecture=architecture,
        requirements=refined_requirements,
        interface_spec=interface_spec or "No interface specification.",
    )
    result = query_llm(prompt, temperature=0.3)

    if not result["success"] or not result.get("content"):
        append_event(
            "tool:project",
            {"action": "create_stories", "success": False, "error": result.get("error")},
        )
        return []

    content = result["content"].strip()
    parsed = _extract_json(content)

    # Resilience: If it's a dict instead of a list, look for common keys like 'stories' or 'user_stories'
    stories = None
    if isinstance(parsed, list):
        stories = parsed
    elif isinstance(parsed, dict):
        for key in ["stories", "user_stories", "data"]:
            if key in parsed and isinstance(parsed[key], list):
                stories = parsed[key]
                break

    if stories is None:
        append_event(
            "tool:project",
            {"action": "create_stories", "success": False, "error": "Failed to parse JSON or response was not a list of stories"},
        )
        return []

    append_event(
        "tool:project",
        {"action": "create_stories", "success": True, "story_count": len(stories)},
    )
    return stories


# ── Backlog Definition Phase ────────────────────────────────────
# Execution lives in tools/sprint_tool.py: sprints pull from the backlog this
# phase persists, and run stories through the gated pipeline.


def _persist_interface_spec(interface_spec: str, goal: str) -> Optional[str]:
    """Write the project's interface spec file and register it. Returns spec_id."""
    if not interface_spec:
        return None
    try:
        import hashlib
        from tools.interface_tool import ensure_specs_dir
        spec_id = f"spec-{hashlib.md5(interface_spec.encode()).hexdigest()[:8]}"
        spec_path = ensure_specs_dir() / f"{spec_id}.yaml"
        spec_path.write_text(interface_spec, encoding="utf-8")
        state = load_project_state()
        if "interface_specs" not in state:
            state["interface_specs"] = {}
        state["interface_specs"][spec_id] = {
            "id": spec_id,
            "goal": goal,
            "generated_at": _now(),
            "file": f"{spec_id}.yaml",
        }
        state["active_spec_id"] = spec_id
        save_project_state(state)
        return spec_id
    except Exception as e:
        append_event(
            "tool:project",
            {"action": "persist_interface_spec", "success": False, "error": str(e)},
        )
        return None


def _persist_backlog_stories(
    story_specs: List[Dict[str, Any]],
    project_id: str,
) -> List[str]:
    """Persist LLM-proposed stories to the durable backlog (define, don't run).

    Task specs are stored as planned_tasks on each story; real registry tasks
    are created only when a sprint executes the story.
    """
    from tools.story_tool import create_story

    created_ids: List[str] = []
    for spec in story_specs:
        planned = [
            {
                "description": t.get("description", ""),
                "role": t.get("role", "developer"),
                "dependencies": [d for d in (t.get("dependencies") or []) if isinstance(d, int)],
            }
            for t in (spec.get("tasks") or [])
            if t.get("description")
        ]
        story = create_story(
            title=spec.get("title", "Untitled story"),
            description=spec.get("description", ""),
            acceptance_criteria=spec.get("acceptance_criteria", []),
            priority=spec.get("priority", "medium"),
            project_id=project_id,
            story_points=spec.get("story_points"),
            planned_tasks=planned,
        )
        created_ids.append(story["id"])
    return created_ids


# ── Public API ──────────────────────────────────────────────────


def create_project_record(description: str, name: str) -> Dict[str, Any]:
    """Creates and saves a new project record on disk. Returns the project dict."""
    project_id = f"proj-{uuid4().hex[:8]}"
    # Use a folder name that includes both the human-readable name and the unique ID
    folder_name = f"{name.replace(' ', '_').lower()}_{project_id}"
    project_dir = USER_PROJECTS_ROOT / folder_name
    project_dir.mkdir(parents=True, exist_ok=True)

    project = {
        "id": project_id,
        "name": name,
        "folder_name": folder_name,
        "description": description,
        "phase": PHASE_BA_ANALYSIS,
        "status": PROJECT_STATUS_ACTIVE,
        "auto_execute": False,
        "refined_requirements": "",
        "architecture": "",
        "interface_spec": "",
        "stories": [],
        "story_ids": [],
        "sprint_ids": [],
        "change_requests": [],
        "results": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    save_json(project_dir / "state.json", project)
    return project

def submit_project(
    description: str,
    name: Optional[str] = None,
    project_id: Optional[str] = None,
    auto_execute: bool = False,
) -> Dict[str, Any]:
    """Submit a high-level project for the definition pipeline.

    Ends with a defined backlog and a proposed sprint awaiting approval.
    With auto_execute=True the proposed sprint starts immediately (the old
    one-shot behavior). If project_id is provided, it resumes/continues an
    existing record — re-analyzing the STORED description, not the argument,
    unless a new non-empty description is passed explicitly.
    """
    ensure_initialized()

    if project_id:
        project = _load_project(project_id)
        if not project:
            return {"success": False, "error": f"Project {project_id} not found"}
        # Resume analyzes the stored description by default (a re-run must
        # not silently re-scope the project from a stale argument).
        if not description:
            description = project.get("description", "")
    else:
        proj_name = name or "Untitled Project"
        project = create_project_record(description, proj_name)
        project["auto_execute"] = bool(auto_execute)
        _save_project(project)
        project_id = project["id"]

    # ── Phase 1: BA Analysis ──
    _broadcast_pipeline_status(project_id, "ba_analysis", "Analyzing requirements...")
    ba_result = _ba_analyze(description)

    if not ba_result["success"]:
        project["status"] = PROJECT_STATUS_FAILED
        project["updated_at"] = _now()
        _save_project(project)
        return {
            "success": False,
            "project_id": project_id,
            "error": ba_result.get("error", "BA analysis failed"),
        }

    project["refined_requirements"] = ba_result["refined_requirements"]

    ambiguities = ba_result.get("ambiguities", [])

    if ambiguities:
        # Create clarification requests for each ambiguity
        from tools.clarification_tool import create_request

        clarification_ids = []
        for amb in ambiguities:
            req = create_request(
                project_id=project_id,
                question=amb["question"],
                context=amb.get("context", amb.get("reason", "")),
                phase=PHASE_BA_ANALYSIS,
                asked_via=["dashboard"],
            )
            clarification_ids.append(req["id"])

            # Also send via Telegram if configured
            try:
                from tools.telegram_bot import send_clarification, is_configured
                if is_configured():
                    msg_id = send_clarification(
                        question=amb["question"],
                        context=amb.get("context", amb.get("reason", "")),
                        request_id=req["id"],
                    )
                    if msg_id:
                        from tools.clarification_tool import set_telegram_metadata
                        cfg = __import__("tools.telegram_bot", fromlist=["get_config"]).get_config()
                        set_telegram_metadata(req["id"], cfg["chat_id"], msg_id)
            except Exception:
                pass

        project["phase"] = PHASE_BA_ANALYSIS
        project["status"] = PROJECT_STATUS_AWAITING_CLARIFICATION
        project["updated_at"] = _now()
        _save_project(project)
        _broadcast_pipeline_status(project_id, PHASE_BA_ANALYSIS, f"Awaiting {len(ambiguities)} clarification(s) from you.")

        return {
            "success": True,
            "project_id": project_id,
            "status": PROJECT_STATUS_AWAITING_CLARIFICATION,
            "phase": PHASE_BA_ANALYSIS,
            "clarification_ids": clarification_ids,
            "ambiguity_count": len(ambiguities),
            "message": f"Project submitted. {len(ambiguities)} clarification(s) needed before proceeding.",
        }

    # No ambiguities — proceed directly to Architect phase
    return _continue_to_architect(project)


def resume_project(project_id: str, auto_answer: bool = False) -> Dict[str, Any]:
    """Resume a project that was paused for clarifications.

    Checks if all clarifications are answered. If so, continues to
    the next phase (Architect design).

    Args:
        project_id: The project to resume.
        auto_answer: If True, answers all pending questions with default text.

    Returns:
        Dict with status and results.
    """
    ensure_initialized()
    project = _load_project(project_id)
    if not project:
        return {"success": False, "error": f"Project {project_id} not found"}

    if project["status"] != PROJECT_STATUS_AWAITING_CLARIFICATION:
        return {
            "success": False,
            "error": f"Project is in state '{project['status']}', not awaiting clarification",
        }

    from tools.clarification_tool import has_pending, answer_all_pending

    if has_pending(project_id):
        if auto_answer:
            count = answer_all_pending(project_id)
            append_event(
                "tool:project",
                {"action": "auto_answered", "project_id": project_id, "count": count},
            )
        else:
            return {
                "success": True,
                "project_id": project_id,
                "status": PROJECT_STATUS_AWAITING_CLARIFICATION,
                "message": "Still awaiting clarification responses.",
            }

    # All clarifications answered (or auto-answered) — continue
    append_event(
        "tool:project",
        {"action": "resume_project", "project_id": project_id, "phase": project["phase"]},
    )

    if project["phase"] == PHASE_BA_ANALYSIS:
        return _continue_to_architect(project)
    elif project["phase"] == PHASE_ARCHITECT_DESIGN:
        return _define_backlog(project)
    else:
        return {
            "success": False,
            "error": f"Unknown phase: {project['phase']}",
        }


def _continue_to_architect(project: Dict[str, Any]) -> Dict[str, Any]:
    """Transition from BA phase to Architect phase."""
    project_id = project["id"]
    project["phase"] = PHASE_ARCHITECT_DESIGN
    project["status"] = PROJECT_STATUS_ACTIVE
    _save_project(project)

    append_event(
        "tool:project",
        {"action": "phase_transition", "project_id": project_id, "phase": PHASE_ARCHITECT_DESIGN},
    )
    _broadcast_pipeline_status(project_id, PHASE_ARCHITECT_DESIGN, "Designing system architecture...")

    # ── Phase 2: Architect Design ──
    arch_result = _architect_design(project["refined_requirements"])

    if not arch_result["success"]:
        project["status"] = PROJECT_STATUS_FAILED
        project["updated_at"] = _now()
        _save_project(project)
        _broadcast_pipeline_status(project_id, "failed", arch_result.get("error", "Architect design failed"))
        return {
            "success": False,
            "project_id": project_id,
            "error": arch_result.get("error", "Architect design failed"),
        }

    project["architecture"] = arch_result["architecture"]
    project["interface_spec"] = arch_result["interface_spec"]

    # Store architecture in project state for reference
    try:
        state = load_project_state()
        state["system_architecture"] = {
            "project_id": project_id,
            "architecture": arch_result["architecture"][:2000],
            "generated_at": _now(),
        }
        save_project_state(state)
    except Exception:
        pass

    _save_project(project)

    # Proceed to backlog definition (execution happens via sprints)
    return _define_backlog(project)


def _define_backlog(project: Dict[str, Any]) -> Dict[str, Any]:
    """Transition from Architect phase to a defined product backlog.

    Persists estimated, project-scoped stories (with deferred task specs) and
    proposes a first sprint from them. Nothing executes here — start_sprint()
    is the PO's approval — unless the project was submitted with auto_execute,
    in which case the proposed sprint starts immediately.
    """
    project_id = project["id"]
    project["phase"] = PHASE_EXECUTION
    project["status"] = PROJECT_STATUS_ACTIVE
    _save_project(project)

    append_event(
        "tool:project",
        {"action": "phase_transition", "project_id": project_id, "phase": PHASE_EXECUTION},
    )
    _broadcast_pipeline_status(project_id, PHASE_EXECUTION, "Defining backlog stories...")

    story_specs = _create_stories_and_tasks(
        architecture=project["architecture"],
        refined_requirements=project["refined_requirements"],
        interface_spec=project["interface_spec"],
    )

    if not story_specs:
        project["status"] = PROJECT_STATUS_FAILED
        project["updated_at"] = _now()
        _save_project(project)
        _broadcast_pipeline_status(project_id, "failed", "Failed to create stories from architecture")
        return {
            "success": False,
            "project_id": project_id,
            "error": "Failed to create stories from architecture",
        }

    _persist_interface_spec(project.get("interface_spec", ""), goal=project.get("name", project_id))
    story_ids = _persist_backlog_stories(story_specs, project_id)

    project["story_ids"] = project.get("story_ids", []) + story_ids
    project["stories"] = project.get("stories", []) + [s.get("title", "") for s in story_specs]
    project["phase"] = PHASE_BACKLOG_READY
    project["updated_at"] = _now()
    _save_project(project)

    append_event(
        "tool:project",
        {"action": "backlog_defined", "project_id": project_id, "stories": len(story_ids)},
    )

    # Propose the first sprint from the fresh backlog.
    from tools.sprint_tool import plan_sprint, start_sprint
    plan = plan_sprint(project_id)
    sprint_id = plan.get("sprint", {}).get("id") if plan.get("success") else None
    if sprint_id:
        project["sprint_ids"] = project.get("sprint_ids", []) + [sprint_id]
        project["updated_at"] = _now()
        _save_project(project)

    execution = None
    if sprint_id and project.get("auto_execute"):
        _broadcast_pipeline_status(project_id, PHASE_EXECUTION, f"Auto-executing {sprint_id}...")
        execution = start_sprint(sprint_id)
        summary = None
        if execution.get("success"):
            results = execution.get("results", [])
            summary = {
                "sprint_id": sprint_id,
                "stories": len(results),
                "stories_done": sum(1 for r in results if r.get("all_succeeded")),
            }
            project["results"] = project.get("results", []) + [summary]
            project["updated_at"] = _now()
            _save_project(project)
        _broadcast_pipeline_status(
            project_id, PHASE_BACKLOG_READY,
            f"{sprint_id} executed; awaiting sprint review." if execution.get("success")
            else f"{sprint_id} execution failed: {execution.get('error')}",
        )
    else:
        _broadcast_pipeline_status(
            project_id, PHASE_BACKLOG_READY,
            f"Backlog ready ({len(story_ids)} stories). "
            + (f"Sprint {sprint_id} proposed — start it to begin work." if sprint_id
               else "No sprint could be proposed."),
        )

    return {
        "success": True,
        "project_id": project_id,
        "status": project["status"],
        "phase": PHASE_BACKLOG_READY,
        "story_ids": story_ids,
        "proposed_sprint_id": sprint_id,
        "auto_executed": bool(execution),
        "summary": {
            "stories": len(story_ids),
            "sprint_id": sprint_id,
        },
    }


# ── Change Requests (delta modifications) ───────────────────────

CHANGE_REQUEST_PROMPT = """You are a Business Analyst handling a change request for an EXISTING project.

Existing refined requirements:
{requirements}

Existing architecture (summary):
{architecture}

Story ledger — what the team has already defined and built:
{ledger}

Previously answered clarifications:
{clarifications}

Change request from the product owner:
{change}

Produce ONLY the NEW work needed: user stories that implement the change,
building on — never re-implementing — what already exists in the ledger.
If the change alters existing behavior, write a story that amends it and set
"supersedes" to the existing story's ID.

Return a JSON object with exactly these keys:
- "impact_summary": One paragraph on how this change affects the existing system.
- "stories": An array of story objects, each with keys: title, description,
  acceptance_criteria (array), priority ("high"|"medium"|"low"), story_points
  (1, 2, 3, 5, or 8), supersedes (an existing STORY-id or null), tasks (array
  of objects with keys: description, role ("developer"|"qa"|"researcher"|
  "architect"), dependencies (array of 0-based integer indices within THIS
  story)).
- "ambiguities": An array of objects with keys "question", "context",
  "reason" for anything needing human clarification; [] if none.

Return ONLY valid JSON, nothing else."""


def _story_ledger(project: Dict[str, Any]) -> str:
    """Human-readable ledger of the project's stories for BA context."""
    from tools.story_tool import get_story, derive_story_status

    lines = []
    for sid in project.get("story_ids", []):
        story = get_story(sid)
        if not story:
            continue
        line = (
            f"- {sid} [{derive_story_status(sid)}] "
            f"({story.get('story_points') or '?'} pts, {story.get('priority', 'medium')}): "
            f"{story.get('title', '')}"
        )
        if story.get("po_acceptance"):
            line += f" | PO: {story['po_acceptance']}"
        lines.append(line)
    return "\n".join(lines) or "No stories defined yet."


def _answered_clarifications(project_id: str) -> str:
    try:
        from tools.clarification_tool import list_requests
        answered = list_requests(project_id=project_id, status="answered")
    except Exception:
        answered = []
    lines = [f"- Q: {r.get('question', '')}\n  A: {r.get('answer', '')}" for r in answered]
    return "\n".join(lines) or "None."


def submit_change_request(project_id: str, change_description: str) -> Dict[str, Any]:
    """Add a modification to an existing project as a backlog delta.

    Unlike re-running the pipeline, the BA sees the project's existing
    requirements, architecture, story ledger, and answered clarifications, and
    produces only the NEW stories the change needs — appended to the backlog,
    never overwriting prior work. Ambiguities pause the change for human
    answers (re-submit after answering; the answers are fed back in).
    """
    ensure_initialized()
    project = _load_project(project_id)
    if not project:
        return {"success": False, "error": f"Project {project_id} not found"}
    if not project.get("refined_requirements") or not project.get("architecture"):
        return {
            "success": False,
            "error": "Project has no defined requirements/architecture yet — "
                     "finish the initial definition pipeline first.",
        }

    cr_id = f"CR-{uuid4().hex[:6]}"
    prompt = CHANGE_REQUEST_PROMPT.format(
        requirements=project.get("refined_requirements", "")[:6000],
        architecture=project.get("architecture", "")[:4000],
        ledger=_story_ledger(project),
        clarifications=_answered_clarifications(project_id),
        change=change_description,
    )
    result = query_llm(prompt, temperature=0.3)
    if not result.get("success") or not result.get("content"):
        return {"success": False, "error": result.get("error", "LLM query failed")}

    parsed = _extract_json(result["content"].strip())
    if not isinstance(parsed, dict):
        return {"success": False, "error": "Failed to parse change analysis JSON"}

    ambiguities = parsed.get("ambiguities") or []
    record = {
        "id": cr_id,
        "description": change_description,
        "impact_summary": parsed.get("impact_summary", ""),
        "status": "awaiting_clarification" if ambiguities else "accepted_into_backlog",
        "story_ids": [],
        "created_at": _now(),
    }

    if ambiguities:
        from tools.clarification_tool import create_request
        clarification_ids = []
        for amb in ambiguities:
            req = create_request(
                project_id=project_id,
                question=amb.get("question", ""),
                context=amb.get("context", amb.get("reason", "")),
                phase="change_request",
                asked_via=["dashboard"],
            )
            clarification_ids.append(req["id"])
        project["change_requests"] = project.get("change_requests", []) + [record]
        project["updated_at"] = _now()
        _save_project(project)
        append_event(
            "tool:project",
            {"action": "change_request", "project_id": project_id, "cr_id": cr_id,
             "status": "awaiting_clarification", "ambiguities": len(ambiguities)},
        )
        return {
            "success": True,
            "project_id": project_id,
            "change_request_id": cr_id,
            "status": "awaiting_clarification",
            "clarification_ids": clarification_ids,
            "message": f"{len(ambiguities)} clarification(s) needed — answer them and re-submit the change.",
        }

    story_specs = parsed.get("stories") or []
    if not story_specs:
        return {"success": False, "error": "Change analysis produced no stories"}

    story_ids = _persist_backlog_stories(story_specs, project_id)
    # Preserve supersedes links on the new records.
    from tools.story_tool import update_story_fields
    for spec, sid in zip(story_specs, story_ids):
        if spec.get("supersedes"):
            update_story_fields(sid, {"supersedes": spec["supersedes"]})

    record["story_ids"] = story_ids
    project["change_requests"] = project.get("change_requests", []) + [record]
    project["story_ids"] = project.get("story_ids", []) + story_ids
    project["stories"] = project.get("stories", []) + [s.get("title", "") for s in story_specs]
    if project.get("phase") in (PHASE_COMPLETE,):
        project["phase"] = PHASE_BACKLOG_READY
    project["updated_at"] = _now()
    _save_project(project)

    append_event(
        "tool:project",
        {"action": "change_request", "project_id": project_id, "cr_id": cr_id,
         "status": "accepted_into_backlog", "stories": len(story_ids)},
    )
    _broadcast_pipeline_status(
        project_id, PHASE_BACKLOG_READY,
        f"Change request {cr_id}: {len(story_ids)} new stories added to the backlog.",
    )
    return {
        "success": True,
        "project_id": project_id,
        "change_request_id": cr_id,
        "status": "accepted_into_backlog",
        "story_ids": story_ids,
        "impact_summary": record["impact_summary"],
    }


def get_project_status(project_id: str) -> Optional[Dict[str, Any]]:
    """Get the current status of a project, including backlog and sprints."""
    project = _load_project(project_id)
    if not project:
        return None

    from tools.clarification_tool import count_by_status, list_requests

    clarification_counts = count_by_status(project_id)
    pending_clarifications = list_requests(project_id=project_id, status="pending")

    results = project.get("results")
    if isinstance(results, list) and results and isinstance(results[-1], dict):
        summary = results[-1]
    elif isinstance(results, dict):
        summary = results
    else:
        summary = {}

    try:
        from tools.story_tool import list_backlog
        open_backlog = len(list_backlog(project_id))
    except Exception:
        open_backlog = None

    sprints = []
    try:
        from tools.sprint_tool import list_sprints
        sprints = [
            {"id": s["id"], "status": s.get("status"), "goal": s.get("goal"),
             "velocity_points": s.get("velocity_points")}
            for s in list_sprints(project_id)
        ]
    except Exception:
        pass

    return {
        "id": project["id"],
        "description": project["description"][:200],
        "phase": project["phase"],
        "status": project["status"],
        "clarifications": {
            "total": sum(clarification_counts.values()),
            "pending": clarification_counts.get("pending", 0),
            "answered": clarification_counts.get("answered", 0),
            "pending_requests": [
                {"id": r["id"], "question": r["question"], "created_at": r["created_at"]}
                for r in pending_clarifications
            ],
        },
        "summary": summary,
        "backlog": {
            "open": open_backlog,
            "total_stories": len(project.get("story_ids", [])),
        },
        "sprints": sprints,
        "change_requests": [
            {"id": c.get("id"), "status": c.get("status")}
            for c in project.get("change_requests", [])
        ],
        "created_at": project.get("created_at", ""),
        "updated_at": project.get("updated_at", ""),
    }


def _cli():
    """Console-script entry point: define a project's backlog from a description."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: sdlc-plan <project-description>")
        sys.exit(1)

    description = " ".join(sys.argv[1:])
    result = submit_project(description)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _cli()
