"""Stage Gate Tool — three-key verification pipeline.

Enforces the quality gate workflow:
  Developer completes → Test Analyst verifies → Architect reviews → Done

Each gate must pass before the task advances. Failures reject the task
back to the developer with attached error logs.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bootstrap import (
    get_task,
    update_task_status,
    append_event,
    load_project_state,
    save_project_state,
)

from tools.llm_tool import query_llm
from tools.kanban_tool import sync_task_column


def _sync_story_status(task_id: int) -> None:
    """Sync parent story status when a task transitions through the pipeline."""
    try:
        from bootstrap import get_task as _gt
        task = _gt(task_id)
        if task and task.get("story_id"):
            from tools.story_tool import sync_all_stories
            sync_all_stories()
    except Exception:
        pass

# ── Stage Gate Statuses ─────────────────────────────────────────

STATUS_PENDING_VERIFICATION = "pending_verification"
STATUS_TESTING = "testing"
STATUS_TESTING_PASSED = "testing_passed"
STATUS_ARCHITECT_REVIEW = "architect_review"
STATUS_COMPLETED = "done"
STATUS_REJECTED = "rejected"

# Gates in order
GATE_SEQUENCE = [
    STATUS_PENDING_VERIFICATION,
    STATUS_TESTING,
    STATUS_TESTING_PASSED,
    STATUS_ARCHITECT_REVIEW,
    STATUS_COMPLETED,
]

# ── Gate Execution ──────────────────────────────────────────────


def submit_for_verification(task_id: int, developer_notes: str = "") -> Dict[str, Any]:
    """Developer signals completion; task enters verification pipeline.

    Moves task to 'pending_verification' and triggers the QA gate.
    """
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}

    update_task_status(task_id, STATUS_PENDING_VERIFICATION, notes=developer_notes)
    _sync_story_status(task_id)
    append_event(
        "tool:stage_gate",
        {"action": "submit_verification", "task_id": task_id, "agent": task.get("agent")},
    )
    return {"success": True, "task_id": task_id, "status": STATUS_PENDING_VERIFICATION}


def run_qa_gate(task_id: int) -> Dict[str, Any]:
    """Execute the QA verification gate.

    Loads the QA role, sends the task description to the LLM for test
    analysis, and passes or rejects the task.

    Returns:
        Dict with success, status, and gate results.
    """
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}

    update_task_status(task_id, STATUS_TESTING)
    sync_task_column(task_id, STATUS_TESTING)

    # Load QA role for system prompt
    from tools.orchestrator_tool import load_role
    qa_role = load_role("qa")
    if not qa_role:
        qa_role = {"system_prompt": "You are a QA analyst. Verify the following task implementation."}

    qa_prompt = QA_GATE_PROMPT.format(
        description=task["description"],
        agent=task.get("agent", "developer"),
        notes=task.get("completion_notes", "No completion notes provided."),
    )

    system_prompt = qa_role["system_prompt"]
    result = query_llm(qa_prompt, system_prompt=system_prompt, temperature=0.2)

    passed = False
    feedback = ""

    if result["success"] and result.get("content"):
        content = result["content"].strip()
        feedback = content[:1000]
        passed = "PASS" in content.upper() or "APPROVE" in content.upper()

    if passed:
        update_task_status(task_id, STATUS_TESTING_PASSED, notes=f"QA passed: {feedback[:300]}")
        sync_task_column(task_id, STATUS_TESTING_PASSED)
        _sync_story_status(task_id)
        append_event(
            "tool:stage_gate",
            {"action": "qa_pass", "task_id": task_id, "feedback": feedback[:500]},
        )
        return {
            "success": True,
            "task_id": task_id,
            "gate": "qa",
            "passed": True,
            "feedback": feedback,
            "status": STATUS_TESTING_PASSED,
        }
    else:
        rejection = feedback or (result.get("error", "QA analysis failed"))
        reject_task(task_id, "qa", rejection)
        append_event(
            "tool:stage_gate",
            {"action": "qa_fail", "task_id": task_id, "feedback": rejection[:500]},
        )
        return {
            "success": False,
            "task_id": task_id,
            "gate": "qa",
            "passed": False,
            "feedback": rejection,
            "status": STATUS_REJECTED,
        }


def run_architect_gate(task_id: int) -> Dict[str, Any]:
    """Execute the architectural review gate.

    Loads the Architect role, reviews the task for structural consistency,
    and approves or rejects.

    Returns:
        Dict with success, status, and gate results.
    """
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}

    update_task_status(task_id, STATUS_ARCHITECT_REVIEW)
    sync_task_column(task_id, STATUS_ARCHITECT_REVIEW)

    # Load Architect role
    from tools.orchestrator_tool import load_role
    arch_role = load_role("architect")
    if not arch_role:
        arch_role = {"system_prompt": "You are a Solution Architect. Review the following task for architectural consistency."}

    state = load_project_state()
    architecture = json.dumps(state.get("system_architecture", {}), indent=2)

    # Load interface spec if associated with the task
    interface_spec = "No interface specification defined."
    spec_id = task.get("interface_spec_id")
    if spec_id:
        try:
            from tools.interface_tool import load_interface_spec
            spec_content = load_interface_spec(spec_id)
            if spec_content:
                interface_spec = spec_content
        except ImportError:
            pass

    arch_prompt = ARCHITECT_GATE_PROMPT.format(
        description=task["description"],
        notes=task.get("completion_notes", "No completion notes."),
        qa_feedback=task.get("verification_artifacts", [""])[0] if task.get("verification_artifacts") else "N/A",
        architecture=architecture or "No architecture defined yet.",
        interface_spec=interface_spec,
    )

    result = query_llm(arch_prompt, system_prompt=arch_role["system_prompt"], temperature=0.2)

    approved = False
    feedback = ""

    if result["success"] and result.get("content"):
        content = result["content"].strip()
        feedback = content[:1000]
        approved = "APPROVE" in content.upper() or "PASS" in content.upper()

    if approved:
        update_task_status(task_id, STATUS_COMPLETED, notes=f"Architect approved: {feedback[:300]}")
        sync_task_column(task_id, STATUS_COMPLETED)
        _sync_story_status(task_id)
        append_event(
            "tool:stage_gate",
            {"action": "architect_pass", "task_id": task_id, "feedback": feedback[:500]},
        )
        return {
            "success": True,
            "task_id": task_id,
            "gate": "architect",
            "passed": True,
            "feedback": feedback,
            "status": STATUS_COMPLETED,
        }
    else:
        rejection = feedback or (result.get("error", "Architect review failed"))
        reject_task(task_id, "architect", rejection)
        append_event(
            "tool:stage_gate",
            {"action": "architect_fail", "task_id": task_id, "feedback": rejection[:500]},
        )
        return {
            "success": False,
            "task_id": task_id,
            "gate": "architect",
            "passed": False,
            "feedback": rejection,
            "status": STATUS_REJECTED,
        }


def run_contract_gate(task_id: int) -> Dict[str, Any]:
    """Execute the interface contract validation gate.

    Validates the implementation against the interface specification.
    If no spec is associated with the task, the gate passes by default.

    Returns:
        Dict with success, status, and gate results.
    """
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}

    spec_id = task.get("interface_spec_id")
    if not spec_id:
        return {
            "success": True,
            "task_id": task_id,
            "gate": "contract",
            "passed": True,
            "feedback": "No interface specification defined. Gate passed.",
            "status": task["status"],
        }

    try:
        from tools.interface_tool import validate_implementation_against_spec
        validation = validate_implementation_against_spec(
            spec_id,
            task.get("completion_notes", "No implementation notes."),
        )

        if not validation["success"]:
            return {
                "success": False,
                "task_id": task_id,
                "gate": "contract",
                "passed": False,
                "feedback": validation["feedback"],
                "status": STATUS_REJECTED,
            }

        if validation["compliant"]:
            append_event(
                "tool:stage_gate",
                {"action": "contract_pass", "task_id": task_id, "spec_id": spec_id},
            )
            return {
                "success": True,
                "task_id": task_id,
                "gate": "contract",
                "passed": True,
                "feedback": validation["feedback"],
                "status": task["status"],
            }
        else:
            rejection = validation["feedback"]
            reject_task(task_id, "contract", rejection)
            append_event(
                "tool:stage_gate",
                {"action": "contract_fail", "task_id": task_id, "spec_id": spec_id, "feedback": rejection[:500]},
            )
            return {
                "success": False,
                "task_id": task_id,
                "gate": "contract",
                "passed": False,
                "feedback": rejection,
                "status": STATUS_REJECTED,
            }
    except ImportError:
        return {
            "success": True,
            "task_id": task_id,
            "gate": "contract",
            "passed": True,
            "feedback": "Contract validation tool unavailable. Gate passed.",
            "status": task["status"],
        }


def reject_task(task_id: int, gate: str, reason: str) -> None:
    """Reject a task back to pending with gate failure notes."""
    task = get_task(task_id)
    if not task:
        return

    rejection_note = f"[REJECTED by {gate}] {reason[:500]}"

    artifact = {
        "gate": gate,
        "result": "failed",
        "reason": reason[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Update via bootstrap to ensure persistence
    from bootstrap import load_json, save_json, TASK_REGISTRY_PATH, TASKS_DIR, append_event as _ae
    registry = load_json(TASK_REGISTRY_PATH, {})
    for t in registry["tasks"]:
        if t["id"] == task_id:
            if "verification_artifacts" not in t:
                t["verification_artifacts"] = []
            t["verification_artifacts"].append(artifact)
            t["status"] = STATUS_REJECTED
            t["completion_notes"] = rejection_note
            t["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_json(TASK_REGISTRY_PATH, registry)
            task_file = TASKS_DIR / f"task_{task_id}.json"
            save_json(task_file, t)
            _ae("system:task_update", {
                "task_id": task_id, "old_status": t.get("status"), "new_status": STATUS_REJECTED,
            })
            break

    sync_task_column(task_id, STATUS_REJECTED)
    _sync_story_status(task_id)


def run_full_pipeline(task_id: int, developer_notes: str = "") -> Dict[str, Any]:
    """Run the complete stage-gate pipeline for a task.

    Steps:
      1. Submit for verification
      2. Run QA gate
      3. If QA passes, run Contract gate (interface spec validation)
      4. If Contract passes, run Architect gate
      5. Return final results

    Returns:
        Dict with pipeline results for each gate.
    """
    results: List[Dict[str, Any]] = []

    # Step 1: Submit
    submission = submit_for_verification(task_id, developer_notes)
    if not submission["success"]:
        return {"success": False, "error": submission["error"], "results": []}
    results.append({"stage": "submission", **submission})

    # Step 2: QA Gate
    qa_result = run_qa_gate(task_id)
    results.append({"stage": "qa_gate", **qa_result})

    if not qa_result["passed"]:
        return {"success": False, "stopped_at": "qa", "results": results}

    # Step 3: Contract Gate
    contract_result = run_contract_gate(task_id)
    results.append({"stage": "contract_gate", **contract_result})

    if not contract_result["passed"]:
        return {"success": False, "stopped_at": "contract", "results": results}

    # Step 4: Architect Gate
    arch_result = run_architect_gate(task_id)
    results.append({"stage": "architect_gate", **arch_result})

    return {
        "success": arch_result["passed"],
        "stopped_at": "architect" if not arch_result["passed"] else None,
        "results": results,
    }


def get_pipeline_status(task_id: int) -> Dict[str, Any]:
    """Return the current pipeline position of a task."""
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found"}

    status = task["status"]
    artifacts = task.get("verification_artifacts", [])

    gates_passed = []
    gates_failed = []

    if status in (STATUS_TESTING_PASSED, STATUS_ARCHITECT_REVIEW, STATUS_COMPLETED):
        gates_passed.append("qa")
    if status == STATUS_COMPLETED:
        gates_passed.append("artifact")

    for art in artifacts:
        if isinstance(art, dict):
            if art.get("result") == "failed":
                gates_failed.append(art.get("gate", "unknown"))

    return {
        "success": True,
        "task_id": task_id,
        "status": status,
        "gates_passed": gates_passed,
        "gates_failed": gates_failed,
        "artifacts": artifacts,
    }


# ── Prompts ─────────────────────────────────────────────────────

QA_GATE_PROMPT = """You are evaluating the following completed task for quality assurance.

Task Description: {description}
Implementing Agent: {agent}
Completion Notes:
{notes}

Evaluate the implementation against these criteria:
1. Does the implementation address the task description fully?
2. Are edge cases and error handling considered?
3. Is the code quality sufficient (readability, conventions, documentation)?
4. Are there obvious security concerns?

Respond with:
PASS - [brief justification]
or
FAIL - [specific issues that need to be fixed]

Be strict but fair. Only fail if there are concrete, fixable issues."""

ARCHITECT_GATE_PROMPT = """You are performing an architectural review of a completed task.

Task Description: {description}
Completion Notes:
{notes}

QA Feedback:
{qa_feedback}

Current System Architecture:
{architecture}

Interface Specification:
{interface_spec}

Evaluate:
1. Does the implementation conform to the system architecture?
2. Are there design pattern violations or inconsistencies?
3. Does the change introduce unwanted coupling or technical debt?
4. Is the implementation compliant with the interface specification?
5. Are there any interface contract violations or gaps?

Respond with:
APPROVE - [brief justification]
or
REJECT - [specific architectural violations to address]

Focus on structural integrity and contract compliance, not code style."""
