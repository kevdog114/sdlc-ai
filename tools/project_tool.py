"""Project Tool — full SDLC pipeline entry point.

Accepts a high-level project description and orchestrates:
  1. BA Phase: Analyze requirements, detect ambiguities, create clarification requests
  2. Architect Phase: Review refined requirements, produce architecture and interface specs
  3. Execution Phase: Create stories/tasks, execute with stage gates (QA → Architect approval)

The pipeline pauses at clarification points and can be resumed once all
ambiguities are resolved (via Telegram or Dashboard).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from bootstrap import (
    BASE_DIR,
    STATE_DIR,
    append_event,
    ensure_initialized,
    load_json,
    load_project_state,
    save_json,
    save_project_state,
)

from tools.llm_tool import query_llm

# --- Configuration ----------------------------------------------
USER_PROJECTS_ROOT = Path("~/dev-projects/sdlc-ai/user_projects").expanduser()

PHASE_BA_ANALYSIS = "ba_analysis"
PHASE_ARCHITECT_DESIGN = "architect_design"
PHASE_EXECUTION = "execution"
PHASE_COMPLETE = "complete"

PROJECT_STATUS_ACTIVE = "active"
PROJECT_STATUS_AWAITING_CLARIFICATION = "awaiting_clarification"
PROJECT_STATUS_COMPLETED = "completed"
PROJECT_STATUS_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _ba_analyze(description: str) -> Dict[str, Any]:
    """Run the BA analysis phase.

    Returns:
        Dict with keys: refined_requirements, ambiguities (list), success (bool)
    """
    prompt = BA_ANALYSIS_PROMPT.format(description=description)
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
    # Clean markdown fences
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()

    try:
        parsed = json.loads(content)
        refined = parsed.get("refined_requirements", description)
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
    except (json.JSONDecodeError, TypeError) as e:
        append_event(
            "tool:project",
            {"action": "ba_analyze", "success": False, "error": f"JSON parse: {e}"},
        )
        return {
            "success": True,
            "refined_requirements": content,
            "ambiguities": [],
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
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()

    try:
        parsed = json.loads(content)
        architecture = parsed.get("architecture", "")
        interface_spec = parsed.get("interface_spec", "")
        append_event(
            "tool:project",
            {"action": "architect_design", "success": True},
        )
        return {"success": True, "architecture": architecture, "interface_spec": interface_spec}
    except (json.JSONDecodeError, TypeError) as e:
        append_event(
            "tool:project",
            {"action": "architect_design", "success": False, "error": f"JSON parse: {e}"},
        )
        return {"success": False, "architecture": "", "interface_spec": "", "error": str(e)}


# ── Story/Task Creation from Architecture ───────────────────────

STORY_CREATION_PROMPT = """You are a Business Analyst. Based on the following architecture and requirements,
break the work into user stories and tasks.

Architecture:
{architecture}

Requirements:
{requirements}

Interface Specification:
{interface_spec}

Create a list of user stories. Each story should have:
- title: A short user story title
- description: Detailed description
- acceptance_criteria: List of acceptance criteria
- priority: high, medium, or low
- tasks: List of tasks, each with:
    - description: Clear actionable task description
    - role: One of: developer, qa, researcher, architect
    - dependencies: List of task indices (0-based) within this story that must complete first

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
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()

    try:
        stories = json.loads(content)
        if not isinstance(stories, list):
            return []
        append_event(
            "tool:project",
            {"action": "create_stories", "success": True, "story_count": len(stories)},
        )
        return stories
    except (json.JSONDecodeError, TypeError):
        return []


# ── Execution Phase ─────────────────────────────────────────────


def _execute_story_tasks(
    story: Dict[str, Any],
    project_id: str,
    interface_spec: str,
) -> Dict[str, Any]:
    """Execute all tasks in a story with stage gates.

    Uses the orchestrator's delegate_task for each task.
    Tasks are executed in dependency order within the story.
    """
    from tools.orchestrator_tool import delegate_task
    from tools.story_tool import create_story, add_task_to_story

    # Create the story in the registry
    s = create_story(
        title=story["title"],
        description=story.get("description", ""),
        acceptance_criteria=story.get("acceptance_criteria", []),
        priority=story.get("priority", "medium"),
    )
    story_id = s["id"]

    # Save interface spec if provided
    spec_id = None
    if interface_spec:
        try:
            import hashlib
            import yaml
            from tools.interface_tool import INTERFACE_SPECS_DIR
            spec_id = f"spec-{hashlib.md5(interface_spec.encode()).hexdigest()[:8]}"
            spec_path = INTERFACE_SPECS_DIR / f"{spec_id}.yaml"
            spec_path.write_text(interface_spec, encoding="utf-8")
            # Register in state
            state = load_project_state()
            if "interface_specs" not in state:
                state["interface_specs"] = {}
            state["interface_specs"][spec_id] = {
                "id": spec_id,
                "goal": story["title"],
                "generated_at": _now(),
                "file": f"{spec_id}.yaml",
            }
            save_project_state(state)
        except Exception:
            pass

    tasks = story.get("tasks", [])
    results = []
    all_succeeded = True
    task_id_map: Dict[int, int] = {}

    for idx, task in enumerate(tasks):
        desc = task["description"]
        role = task.get("role", "developer")
        deps = task.get("dependencies", [])
        dep_ids = [task_id_map.get(d) for d in deps if d in task_id_map]
        context = f"Project: {project_id}"
        if dep_ids:
            context += f"\nDependencies (task IDs): {dep_ids}"

        result = delegate_task(
            task_description=desc,
            role_name=role,
            context=context,
            dependencies=dep_ids,
            story_id=story_id,
            interface_spec_id=spec_id,
        )
        tid = result.get("task_id")
        if tid is not None:
            task_id_map[idx] = tid
            try:
                add_task_to_story(story_id, tid)
            except Exception:
                pass

        results.append({
            "index": idx,
            "description": desc,
            "role": role,
            "task_id": tid,
            "success": result.get("success", False),
            "error": result.get("error"),
        })

        if not result.get("success", False):
            all_succeeded = False

    return {
        "story_id": story_id,
        "title": story["title"],
        "task_count": len(tasks),
        "succeeded": sum(1 for r in results if r["success"]),
        "failed": sum(1 for r in results if not r["success"]),
        "all_succeeded": all_succeeded,
        "task_results": results,
    }


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
        "refined_requirements": "",
        "architecture": "",
        "interface_spec": "",
        "stories": [],
        "results": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    save_json(project_dir / "state.json", project)
    return project

def submit_project(description: str, name: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
    """Submit a high-level project for the full SDLC pipeline.
    If project_id is provided, it resumes/continues an existing record.
    """
    ensure_initialized()

    if project_id:
        project = _load_project(project_id)
        if not project:
            return {"success": False, "error": f"Project {project_id} not found"}
    else:
        proj_name = name or "Untitled Project"
        project = create_project_record(description, proj_name)
        project_id = project["id"]

    # ── Phase 1: BA Analysis ──
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


def resume_project(project_id: str) -> Dict[str, Any]:
    """Resume a project that was paused for clarifications.

    Checks if all clarifications are answered. If so, continues to
    the next phase (Architect design).

    Args:
        project_id: The project to resume.

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

    from tools.clarification_tool import has_pending

    if has_pending(project_id):
        return {
            "success": True,
            "project_id": project_id,
            "status": PROJECT_STATUS_AWAITING_CLARIFICATION,
            "message": "Still awaiting clarification responses.",
        }

    # All clarifications answered — continue
    append_event(
        "tool:project",
        {"action": "resume_project", "project_id": project_id, "phase": project["phase"]},
    )

    if project["phase"] == PHASE_BA_ANALYSIS:
        return _continue_to_architect(project)
    elif project["phase"] == PHASE_ARCHITECT_DESIGN:
        return _continue_to_execution(project)
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

    # ── Phase 2: Architect Design ──
    arch_result = _architect_design(project["refined_requirements"])

    if not arch_result["success"]:
        project["status"] = PROJECT_STATUS_FAILED
        project["updated_at"] = _now()
        _save_project(project)
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

    # Proceed to execution
    return _continue_to_execution(project)


def _continue_to_execution(project: Dict[str, Any]) -> Dict[str, Any]:
    """Transition from Architect phase to Execution phase."""
    project_id = project["id"]
    project["phase"] = PHASE_EXECUTION
    project["status"] = PROJECT_STATUS_ACTIVE
    _save_project(project)

    append_event(
        "tool:project",
        {"action": "phase_transition", "project_id": project_id, "phase": PHASE_EXECUTION},
    )

    # ── Phase 3: Create Stories & Tasks ──
    stories = _create_stories_and_tasks(
        architecture=project["architecture"],
        refined_requirements=project["refined_requirements"],
        interface_spec=project["interface_spec"],
    )

    if not stories:
        project["status"] = PROJECT_STATUS_FAILED
        project["updated_at"] = _now()
        _save_project(project)
        return {
            "success": False,
            "project_id": project_id,
            "error": "Failed to create stories from architecture",
        }

    # ── Execute each story ──
    results = []
    total_tasks = 0
    total_succeeded = 0
    total_failed = 0

    for story in stories:
        story_result = _execute_story_tasks(
            story=story,
            project_id=project_id,
            interface_spec=project.get("interface_spec", ""),
        )
        results.append(story_result)
        total_tasks += story_result["task_count"]
        total_succeeded += story_result["succeeded"]
        total_failed += story_result["failed"]

    project["stories"] = [s.get("title", "") for s in stories]
    project["results"] = results
    project["phase"] = PHASE_COMPLETE
    project["status"] = PROJECT_STATUS_COMPLETED
    project["updated_at"] = _now()
    _save_project(project)

    append_event(
        "tool:project",
        {
            "action": "project_complete",
            "project_id": project_id,
            "stories": len(stories),
            "total_tasks": total_tasks,
            "succeeded": total_succeeded,
            "failed": total_failed,
        },
    )

    return {
        "success": True,
        "project_id": project_id,
        "status": PROJECT_STATUS_COMPLETED,
        "phase": PHASE_COMPLETE,
        "summary": {
            "stories": len(stories),
            "total_tasks": total_tasks,
            "succeeded": total_succeeded,
            "failed": total_failed,
        },
        "story_results": results,
    }


def get_project_status(project_id: str) -> Optional[Dict[str, Any]]:
    """Get the current status of a project."""
    project = _load_project(project_id)
    if not project:
        return None

    from tools.clarification_tool import count_by_status, list_requests

    clarification_counts = count_by_status(project_id)
    pending_clarifications = list_requests(project_id=project_id, status="pending")

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
        "summary": {
            k: v for k, v in project.get("results", {}).items()
            if k in ("stories", "total_tasks", "succeeded", "failed")
        } if isinstance(project.get("results"), dict) else project.get("results")[-1]["summary"] if project.get("results") and isinstance(project.get("results"), list) else {},
        "created_at": project.get("created_at", ""),
        "updated_at": project.get("updated_at", ""),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m tools.project_tool <project-description>")
        sys.exit(1)

    description = " ".join(sys.argv[1:])
    result = submit_project(description)
    print(json.dumps(result, indent=2, default=str))
