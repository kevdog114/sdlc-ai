"""Interface Tool — contract-first specification management.

Generates, stores, loads, and validates interface specifications (OpenAPI/GraphQL)
that serve as the source of truth for component contracts.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from bootstrap import (
    BASE_DIR,
    append_event,
    load_project_state,
    save_project_state,
)

from tools.llm_tool import query_llm

INTERFACE_SPECS_DIR = BASE_DIR / "state" / "interface_specs"

SPEC_GENERATION_PROMPT = (
    "You are a Solution Architect. Generate an interface specification for the "
    "following project goal and its decomposed subtasks.\n\n"
    "Goal: {goal}\n\n"
    "Subtasks:\n{subtasks}\n\n"
    "Generate a minimal OpenAPI 3.0 specification that defines the contracts "
    "between all components. Include:\n"
    "- All API endpoints with methods, paths, request/response schemas\n"
    "- Data models and shared types\n"
    "- Authentication requirements if applicable\n"
    "- Error response formats\n\n"
    "If the project is frontend-only or doesn't use HTTP APIs, generate a "
    "component interface specification instead, defining:\n"
    "- Component props and events\n"
    "- Shared data types and interfaces\n"
    "- Service contracts between modules\n\n"
    "Return ONLY valid YAML, nothing else."
)

SPEC_GAP_PROMPT = (
    "You are a Solution Architect reviewing an interface gap request.\n\n"
    "Current interface specification:\n{current_spec}\n\n"
    "The developer needs the following change:\n{gap_description}\n\n"
    "Task context: {task_description}\n\n"
    "Update the specification to accommodate the requested change while "
    "maintaining consistency. Return ONLY the updated YAML specification."
)

SPEC_VALIDATION_PROMPT = (
    "You are a Solution Architect validating an implementation against an "
    "interface specification.\n\n"
    "Interface specification:\n{spec}\n\n"
    "Implementation notes:\n{implementation_notes}\n\n"
    "Check if the implementation satisfies the interface contract. Look for:\n"
    "- Missing endpoints or interfaces\n"
    "- Schema mismatches in request/response types\n"
    "- Missing required fields\n"
    "- Deviations from the defined contract\n\n"
    "Respond with:\n"
    "COMPLIANT - [brief justification]\n"
    "or\n"
    "NON_COMPLIANT - [specific violations that need to be fixed]\n\n"
    "Be strict about contract adherence."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_specs_dir() -> Path:
    """Create the interface specs directory if it doesn't exist."""
    INTERFACE_SPECS_DIR.mkdir(parents=True, exist_ok=True)
    return INTERFACE_SPECS_DIR


def _get_spec_filename(spec_id: str) -> str:
    return f"{spec_id}.yaml"


def _get_spec_path(spec_id: str) -> Path:
    return ensure_specs_dir() / _get_spec_filename(spec_id)


def generate_interface_spec(goal: str, subtasks: List[Dict[str, Any]]) -> Optional[str]:
    """Generate an interface specification from a goal and its subtasks.

    Uses the LLM to produce an OpenAPI or component interface YAML spec,
    stores it in the interface_specs directory, and registers it in the
    project state.

    Returns:
        The spec_id if successful, None on failure.
    """
    subtasks_text = "\n".join(
        f"  {i+1}. [{st.get('role', 'unknown')}] {st['task']}"
        for i, st in enumerate(subtasks)
    )

    prompt = SPEC_GENERATION_PROMPT.format(
        goal=goal,
        subtasks=subtasks_text,
    )

    result = query_llm(prompt, temperature=0.3)

    if not result["success"] or not result.get("content"):
        append_event(
            "tool:interface",
            {"action": "generate_spec", "goal": goal, "success": False, "error": result.get("error")},
        )
        return None

    spec_content = result["content"].strip()

    # Clean markdown code fences if present
    if spec_content.startswith("```"):
        lines = spec_content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        spec_content = "\n".join(lines).strip()

    # Validate it's parseable YAML
    try:
        parsed = yaml.safe_load(spec_content)
        if not isinstance(parsed, dict):
            raise ValueError("Spec is not a YAML mapping")
    except Exception as e:
        append_event(
            "tool:interface",
            {"action": "generate_spec", "goal": goal, "success": False, "error": f"Invalid YAML: {e}"},
        )
        return None

    # Generate spec ID from goal hash
    import hashlib
    spec_id = f"spec-{hashlib.md5(goal.encode()).hexdigest()[:8]}"

    # Store the spec file
    spec_path = _get_spec_path(spec_id)
    spec_path.write_text(spec_content, encoding="utf-8")

    # Register in project state
    state = load_project_state()
    if "interface_specs" not in state:
        state["interface_specs"] = {}

    state["interface_specs"][spec_id] = {
        "id": spec_id,
        "goal": goal,
        "generated_at": _now(),
        "subtask_count": len(subtasks),
        "file": _get_spec_filename(spec_id),
    }
    save_project_state(state)

    append_event(
        "tool:interface",
        {"action": "generate_spec", "spec_id": spec_id, "goal": goal, "success": True},
    )
    return spec_id


def load_interface_spec(spec_id: str) -> Optional[str]:
    """Load an interface specification by ID.

    Returns:
        The raw YAML content, or None if not found.
    """
    spec_path = _get_spec_path(spec_id)
    if not spec_path.is_file():
        return None
    return spec_path.read_text(encoding="utf-8")


def load_interface_spec_parsed(spec_id: str) -> Optional[Dict[str, Any]]:
    """Load and parse an interface specification by ID.

    Returns:
        The parsed dict, or None if not found or invalid.
    """
    content = load_interface_spec(spec_id)
    if not content:
        return None
    try:
        return yaml.safe_load(content)
    except Exception:
        return None


def get_active_spec() -> Optional[str]:
    """Get the ID of the most recently generated interface spec.

    Returns:
        The spec_id, or None if no specs exist.
    """
    state = load_project_state()
    specs = state.get("interface_specs", {})
    if not specs:
        return None
    # Return the most recently generated spec
    latest = max(specs.values(), key=lambda s: s.get("generated_at", ""))
    return latest.get("id")


def set_active_spec(spec_id: str) -> None:
    """Mark a spec as the active one for the current project."""
    state = load_project_state()
    state["active_spec_id"] = spec_id
    save_project_state(state)
    append_event(
        "tool:interface",
        {"action": "set_active_spec", "spec_id": spec_id},
    )


def get_task_spec_id(task_id: int) -> Optional[str]:
    """Get the interface spec ID associated with a task.

    Returns:
        The spec_id, or None if not associated.
    """
    from bootstrap import get_task
    task = get_task(task_id)
    if not task:
        return None
    return task.get("interface_spec_id")


def update_interface_spec(
    spec_id: str,
    gap_description: str,
    task_description: str = "",
) -> bool:
    """Update an interface specification to address an identified gap.

    Uses the LLM to produce an updated spec that incorporates the requested
    change while maintaining consistency.

    Returns:
        True if the spec was successfully updated.
    """
    current_spec = load_interface_spec(spec_id)
    if not current_spec:
        append_event(
            "tool:interface",
            {"action": "update_spec", "spec_id": spec_id, "success": False, "error": "spec not found"},
        )
        return False

    prompt = SPEC_GAP_PROMPT.format(
        current_spec=current_spec,
        gap_description=gap_description,
        task_description=task_description,
    )

    result = query_llm(prompt, temperature=0.3)

    if not result["success"] or not result.get("content"):
        append_event(
            "tool:interface",
            {"action": "update_spec", "spec_id": spec_id, "success": False, "error": result.get("error")},
        )
        return False

    updated_content = result["content"].strip()

    # Clean markdown code fences
    if updated_content.startswith("```"):
        lines = updated_content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        updated_content = "\n".join(lines).strip()

    # Validate YAML
    try:
        parsed = yaml.safe_load(updated_content)
        if not isinstance(parsed, dict):
            raise ValueError("Updated spec is not a YAML mapping")
    except Exception as e:
        append_event(
            "tool:interface",
            {"action": "update_spec", "spec_id": spec_id, "success": False, "error": f"Invalid YAML: {e}"},
        )
        return False

    # Overwrite the spec file
    spec_path = _get_spec_path(spec_id)
    spec_path.write_text(updated_content, encoding="utf-8")

    # Update project state
    state = load_project_state()
    if spec_id in state.get("interface_specs", {}):
        state["interface_specs"][spec_id]["updated_at"] = _now()
        state["interface_specs"][spec_id]["gap_updates"] = (
            state["interface_specs"][spec_id].get("gap_updates", 0) + 1
        )
        save_project_state(state)

    append_event(
        "tool:interface",
        {"action": "update_spec", "spec_id": spec_id, "gap": gap_description[:200], "success": True},
    )
    return True


def validate_implementation_against_spec(
    spec_id: str,
    implementation_notes: str,
) -> Dict[str, Any]:
    """Validate an implementation against its interface specification.

    Uses the LLM to check whether the implementation satisfies the contract.

    Returns:
        Dict with compliant (bool), feedback (str), and success (bool).
    """
    spec_content = load_interface_spec(spec_id)
    if not spec_content:
        return {
            "success": False,
            "compliant": False,
            "feedback": f"Interface spec {spec_id} not found",
        }

    prompt = SPEC_VALIDATION_PROMPT.format(
        spec=spec_content,
        implementation_notes=implementation_notes,
    )

    result = query_llm(prompt, temperature=0.2)

    if not result["success"] or not result.get("content"):
        return {
            "success": False,
            "compliant": False,
            "feedback": result.get("error", "Validation failed"),
        }

    content = result["content"].strip()
    feedback = content[:1000]
    compliant = "COMPLIANT" in content.upper() and "NON_COMPLIANT" not in content.upper()

    append_event(
        "tool:interface",
        {
            "action": "validate_spec",
            "spec_id": spec_id,
            "compliant": compliant,
            "success": True,
        },
    )
    return {
        "success": True,
        "compliant": compliant,
        "feedback": feedback,
    }


def list_specs() -> List[Dict[str, Any]]:
    """Return all registered interface specifications."""
    state = load_project_state()
    specs = state.get("interface_specs", {})
    return list(specs.values())


def delete_spec(spec_id: str) -> bool:
    """Delete an interface specification file and remove from state."""
    spec_path = _get_spec_path(spec_id)
    if spec_path.is_file():
        spec_path.unlink()

    state = load_project_state()
    if spec_id in state.get("interface_specs", {}):
        del state["interface_specs"][spec_id]
        save_project_state(state)

    append_event(
        "tool:interface",
        {"action": "delete_spec", "spec_id": spec_id},
    )
    return True
