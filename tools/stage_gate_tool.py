"""Stage Gate Tool — three-key verification pipeline.

Enforces the quality gate workflow:
  Developer completes → Test Analyst verifies → Architect reviews → Done

Each gate must pass before the task advances. Failures reject the task
back to the developer with attached error logs.

Gate verdicts are parsed fail-closed: only an explicit verdict token at the
start of a response line counts, and an unparseable response is a failure.
The QA gate additionally executes the target project's automated tests when
they exist — a test failure rejects the task before any LLM opinion is asked.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# ── Verdict Parsing ─────────────────────────────────────────────

_VERDICT_PREFIXES = ("VERDICT:", "RESULT:", "DECISION:", "OUTCOME:", "FINAL VERDICT:")
_NEGATION_PREFIXES = (
    "NOT ", "CANNOT ", "CAN'T ", "DOES NOT ", "DID NOT ", "DO NOT ", "WILL NOT ",
)

QA_PASS_TOKENS = ("PASS", "PASSED", "PASSES")
QA_FAIL_TOKENS = ("FAIL", "FAILED", "FAILS", "REJECT", "REJECTED")
ARCH_PASS_TOKENS = ("APPROVE", "APPROVED")
ARCH_FAIL_TOKENS = ("REJECT", "REJECTED", "FAIL", "FAILED", "DISAPPROVE", "DISAPPROVED")


def parse_gate_verdict(
    content: Optional[str],
    pass_tokens: Tuple[str, ...],
    fail_tokens: Tuple[str, ...],
    scan_lines: int = 5,
) -> Tuple[bool, bool]:
    """Parse an LLM gate response into a pass/fail verdict, failing closed.

    The gate prompts mandate a leading verdict token ("PASS - ..."). Only a
    token found at the *start* of one of the first few non-empty lines counts
    (after stripping markdown decoration and "VERDICT:"-style prefixes);
    verdict words buried in prose — "the tests do not pass", "I cannot
    approve this" — are ignored. Simple leading negations ("NOT APPROVED")
    invert to a failure. A response with no parseable verdict is treated as
    NOT passed: a quality gate must never pass on ambiguity.

    Returns:
        (passed, matched) — matched is False when no verdict token was found.
    """
    if not content:
        return False, False

    lines = [ln for ln in (raw.strip() for raw in content.splitlines()) if ln]
    for line in lines[:scan_lines]:
        # Strip markdown/emoji/list decoration up to the first letter.
        cleaned = re.sub(r"^[^A-Z]+", "", line.upper())
        for prefix in _VERDICT_PREFIXES:
            if cleaned.startswith(prefix):
                cleaned = re.sub(r"^[^A-Z]+", "", cleaned[len(prefix):])
                break

        negated = False
        for neg in _NEGATION_PREFIXES:
            if cleaned.startswith(neg):
                negated = True
                cleaned = cleaned[len(neg):].lstrip()
                break

        match = re.match(r"[A-Z_']+", cleaned)
        if not match:
            continue
        word = match.group(0).rstrip("'")
        if word in fail_tokens:
            return False, True
        if word in pass_tokens:
            return (not negated), True
        # A line that starts with words but no verdict token: keep scanning.

    return False, False


# ── Verification Artifacts ──────────────────────────────────────


def _append_verification_artifact(task_id: int, artifact: Dict[str, Any]) -> None:
    """Attach a gate artifact (pass or fail) to the task record."""
    from bootstrap import load_json, save_json, TASK_REGISTRY_PATH, TASKS_DIR
    registry = load_json(TASK_REGISTRY_PATH, {})
    for t in registry.get("tasks", []):
        if t["id"] == task_id:
            t.setdefault("verification_artifacts", []).append(artifact)
            t["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_json(TASK_REGISTRY_PATH, registry)
            save_json(TASKS_DIR / f"task_{task_id}.json", t)
            return


def _latest_qa_artifact(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for art in reversed(task.get("verification_artifacts") or []):
        if isinstance(art, dict) and art.get("gate") == "qa":
            return art
    return None


# ── Automated Test Execution ────────────────────────────────────

DEFAULT_TEST_TIMEOUT = 300


def _resolve_project_dir(task: Dict[str, Any]) -> Optional[Path]:
    """Best-effort resolution of the target project's working directory."""
    path = task.get("project_path")
    if path and Path(path).is_dir():
        return Path(path)

    project_id = task.get("project_id")
    if project_id:
        try:
            from tools.project_tool import _load_project, USER_PROJECTS_ROOT
            project = _load_project(project_id)
            if project and project.get("folder_name"):
                candidate = USER_PROJECTS_ROOT / project["folder_name"]
                if candidate.is_dir():
                    return candidate
        except Exception:
            return None
    return None


def _detect_test_command(project_dir: Path) -> Optional[List[str]]:
    """Pick a test command for the project, or None when no tests exist."""
    has_pytest_config = (project_dir / "pytest.ini").exists()
    has_test_files = (
        any(project_dir.glob("tests/test_*.py"))
        or any(project_dir.glob("test_*.py"))
    )
    if has_pytest_config or has_test_files:
        return [sys.executable, "-m", "pytest", "-q"]

    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
            test_script = scripts.get("test", "")
            if test_script and "no test specified" not in test_script:
                return ["npm", "test", "--silent"]
        except (json.JSONDecodeError, OSError):
            pass
    return None


def run_automated_tests(task: Dict[str, Any], timeout: int = DEFAULT_TEST_TIMEOUT) -> Dict[str, Any]:
    """Execute the target project's automated tests, if any.

    Runs the detected test command as an argv list (never shell=True) inside
    the project directory. When no project directory or no tests can be
    found, says so honestly instead of pretending a pass.

    Returns:
        Dict with keys: ran (bool), passed (bool|None), command, returncode,
        output, reason.
    """
    project_dir = _resolve_project_dir(task)
    if not project_dir:
        return {"ran": False, "passed": None, "reason": "no project directory resolved"}

    command = _detect_test_command(project_dir)
    if not command:
        return {"ran": False, "passed": None, "reason": "no automated tests found"}

    try:
        proc = subprocess.run(
            command,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ran": True,
            "passed": False,
            "command": " ".join(command),
            "returncode": None,
            "output": f"Test run timed out after {timeout}s",
            "reason": "timeout",
        }
    except OSError as e:
        return {"ran": False, "passed": None, "reason": f"could not run tests: {e}"}

    output = (proc.stdout + "\n" + proc.stderr).strip()
    return {
        "ran": True,
        "passed": proc.returncode == 0,
        "command": " ".join(command),
        "returncode": proc.returncode,
        "output": output[-4000:],
    }


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

    First runs the target project's automated tests (when any exist) — a
    failing test run rejects the task outright, no LLM opinion involved.
    The LLM review then judges the work with the real test results in front
    of it, and its verdict is parsed fail-closed.

    Returns:
        Dict with success, status, and gate results.
    """
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found", "passed": False}

    update_task_status(task_id, STATUS_TESTING)

    # 1. Run real tests before asking anyone's opinion.
    test_run = run_automated_tests(task)
    test_artifact = {
        "gate": "qa",
        "kind": "automated_tests",
        "result": ("passed" if test_run.get("passed") else "failed") if test_run.get("ran") else "not_run",
        "command": test_run.get("command"),
        "returncode": test_run.get("returncode"),
        "output": (test_run.get("output") or test_run.get("reason", ""))[:2000],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _append_verification_artifact(task_id, test_artifact)

    if test_run.get("ran") and not test_run.get("passed"):
        rejection = (
            f"Automated tests failed ({test_run.get('command')}, "
            f"exit {test_run.get('returncode')}):\n{test_run.get('output', '')[:800]}"
        )
        reject_task(task_id, "qa", rejection)
        append_event(
            "tool:stage_gate",
            {"action": "qa_fail", "task_id": task_id, "tests_ran": True, "feedback": rejection[:500]},
        )
        return {
            "success": False,
            "task_id": task_id,
            "gate": "qa",
            "passed": False,
            "feedback": rejection,
            "tests": test_run,
            "status": STATUS_REJECTED,
        }

    if test_run.get("ran"):
        test_summary = (
            f"Automated tests PASSED ({test_run.get('command')}).\n"
            f"Output (tail):\n{test_run.get('output', '')[:1500]}"
        )
    else:
        test_summary = f"No automated tests were executed: {test_run.get('reason', 'unknown')}."

    # 2. LLM review with the test evidence in front of it.
    from tools.orchestrator_tool import load_role
    qa_role = load_role("qa")
    if not qa_role:
        qa_role = {"system_prompt": "You are a QA analyst. Verify the following task implementation."}

    qa_prompt = QA_GATE_PROMPT.format(
        description=task["description"],
        agent=task.get("agent", "developer"),
        notes=task.get("completion_notes") or "No completion notes provided.",
        test_results=test_summary,
    )

    result = query_llm(qa_prompt, system_prompt=qa_role["system_prompt"], temperature=0.2)

    passed = False
    matched = False
    feedback = ""

    if result["success"] and result.get("content"):
        content = result["content"].strip()
        feedback = content[:1000]
        passed, matched = parse_gate_verdict(content, QA_PASS_TOKENS, QA_FAIL_TOKENS)

    if passed:
        _append_verification_artifact(task_id, {
            "gate": "qa",
            "kind": "llm_review",
            "result": "passed",
            "reason": feedback[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        update_task_status(task_id, STATUS_TESTING_PASSED, notes=f"QA passed: {feedback[:300]}")
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
            "tests": test_run,
            "status": STATUS_TESTING_PASSED,
        }
    else:
        if result["success"] and feedback and not matched:
            rejection = f"QA verdict unparseable (fail-closed): {feedback[:500]}"
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
            "tests": test_run,
            "status": STATUS_REJECTED,
        }


def run_architect_gate(task_id: int, qa_feedback: Optional[str] = None) -> Dict[str, Any]:
    """Execute the architectural review gate.

    Loads the Architect role, reviews the task for structural consistency,
    and approves or rejects. `qa_feedback` should carry the QA gate's actual
    verdict for this run; when absent, the latest QA artifact is used.

    Returns:
        Dict with success, status, and gate results.
    """
    task = get_task(task_id)
    if not task:
        return {"success": False, "error": f"Task {task_id} not found", "passed": False}

    update_task_status(task_id, STATUS_ARCHITECT_REVIEW)

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

    if qa_feedback is None:
        qa_art = _latest_qa_artifact(task)
        qa_feedback = (
            f"[{qa_art.get('result', 'unknown')}] {qa_art.get('reason') or qa_art.get('output', '')}"[:800]
            if qa_art else "N/A"
        )

    arch_prompt = ARCHITECT_GATE_PROMPT.format(
        description=task["description"],
        notes=task.get("completion_notes") or "No completion notes.",
        qa_feedback=qa_feedback,
        architecture=architecture or "No architecture defined yet.",
        interface_spec=interface_spec,
    )

    result = query_llm(arch_prompt, system_prompt=arch_role["system_prompt"], temperature=0.2)

    approved = False
    matched = False
    feedback = ""

    if result["success"] and result.get("content"):
        content = result["content"].strip()
        feedback = content[:1000]
        approved, matched = parse_gate_verdict(content, ARCH_PASS_TOKENS, ARCH_FAIL_TOKENS)

    if approved:
        _append_verification_artifact(task_id, {
            "gate": "architect",
            "kind": "llm_review",
            "result": "passed",
            "reason": feedback[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        update_task_status(task_id, STATUS_COMPLETED, notes=f"Architect approved: {feedback[:300]}")
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
        if result["success"] and feedback and not matched:
            rejection = f"Architect verdict unparseable (fail-closed): {feedback[:500]}"
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
        return {"success": False, "error": f"Task {task_id} not found", "passed": False}

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
            task.get("completion_notes") or "No implementation notes.",
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
    for t in registry.get("tasks", []):
        if t["id"] == task_id:
            old_status = t.get("status")
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
                "task_id": task_id, "old_status": old_status, "new_status": STATUS_REJECTED,
            })
            break

    sync_task_column(task_id, STATUS_REJECTED)
    _sync_story_status(task_id)


def run_full_pipeline(task_id: int, developer_notes: str = "") -> Dict[str, Any]:
    """Run the complete stage-gate pipeline for a task.

    Steps:
      1. Submit for verification
      2. Run QA gate (automated tests + reviewed verdict)
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

    if not qa_result.get("passed", False):
        return {"success": False, "stopped_at": "qa", "results": results}

    # Step 3: Contract Gate
    contract_result = run_contract_gate(task_id)
    results.append({"stage": "contract_gate", **contract_result})

    if not contract_result.get("passed", False):
        return {"success": False, "stopped_at": "contract", "results": results}

    # Step 4: Architect Gate — fed this run's actual QA verdict.
    arch_result = run_architect_gate(task_id, qa_feedback=qa_result.get("feedback"))
    results.append({"stage": "architect_gate", **arch_result})

    return {
        "success": arch_result.get("passed", False),
        "stopped_at": "architect" if not arch_result.get("passed", False) else None,
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
    if status in (STATUS_ARCHITECT_REVIEW, STATUS_COMPLETED):
        gates_passed.append("contract")
    if status == STATUS_COMPLETED:
        gates_passed.append("architect")

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

Automated Test Results:
{test_results}

Evaluate the implementation against these criteria:
1. Does the implementation address the task description fully?
2. Are edge cases and error handling considered?
3. Is the code quality sufficient (readability, conventions, documentation)?
4. Are there obvious security concerns?

RESPONSE FORMAT (mandatory): the FIRST LINE of your response must be exactly one
of these two words, followed by " - " and your justification:
PASS - [brief justification]
FAIL - [specific issues that need to be fixed]

Do not write anything before the verdict word. Be strict but fair. Only fail
if there are concrete, fixable issues."""

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

RESPONSE FORMAT (mandatory): the FIRST LINE of your response must be exactly one
of these two words, followed by " - " and your justification:
APPROVE - [brief justification]
REJECT - [specific architectural violations to address]

Do not write anything before the verdict word. Focus on structural integrity
and contract compliance, not code style."""
