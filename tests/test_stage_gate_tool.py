"""Tests for tools/stage_gate_tool.py — three-key verification pipeline."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap
from tools import stage_gate_tool
from tools.stage_gate_tool import (
    submit_for_verification,
    run_qa_gate,
    run_architect_gate,
    run_contract_gate,
    run_full_pipeline,
    get_pipeline_status,
    reject_task,
    STATUS_PENDING_VERIFICATION,
    STATUS_TESTING,
    STATUS_TESTING_PASSED,
    STATUS_ARCHITECT_REVIEW,
    STATUS_COMPLETED,
    STATUS_REJECTED,
)


class _TestSetup:
    """Shared setup/teardown for stage gate tests."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self.tmpdir) / "state"
        self.state_dir.mkdir()
        self.tasks_dir = Path(self.tmpdir) / "tasks"
        self.tasks_dir.mkdir()

        bootstrap.BASE_DIR = Path(self.tmpdir)
        bootstrap.STATE_DIR = self.state_dir
        bootstrap.TASK_REGISTRY_PATH = self.state_dir / "task_registry.json"
        bootstrap.STATE_FILE_PATH = self.state_dir / "project_state.json"
        bootstrap.EVENT_LOG_PATH = self.state_dir / "event_log.jsonl"
        bootstrap.TASKS_DIR = self.tasks_dir
        bootstrap.STORY_REGISTRY_PATH = self.state_dir / "story_registry.json"

        import tools.story_tool as _st
        _st.STORY_REGISTRY_PATH = bootstrap.STORY_REGISTRY_PATH
        _st.BASE_DIR = Path(self.tmpdir)

        bootstrap.init_task_registry()
        bootstrap.init_project_state()
        bootstrap.init_story_registry()

    def _create_task(self, status="pending", agent="developer"):
        return bootstrap.add_task(
            description="Test task",
            agent=agent,
            status=status,
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ── Submit for Verification ──────────────────────────────────────


class TestSubmitForVerification(_TestSetup):
    def test_submit_moves_to_pending_verification(self):
        task = self._create_task()
        result = submit_for_verification(task["id"], "Ready for review")
        assert result["success"] is True
        assert result["status"] == STATUS_PENDING_VERIFICATION

        updated = bootstrap.get_task(task["id"])
        assert updated["status"] == STATUS_PENDING_VERIFICATION

    def test_submit_with_notes(self):
        task = self._create_task()
        submit_for_verification(task["id"], "All tests pass locally")
        updated = bootstrap.get_task(task["id"])
        assert updated["completion_notes"] == "All tests pass locally"

    def test_submit_nonexistent_task(self):
        result = submit_for_verification(999, "notes")
        assert result["success"] is False
        assert "not found" in result["error"]


# ── QA Gate ──────────────────────────────────────────────────────


class TestRunQAGate(_TestSetup):
    def test_qa_pass(self):
        task = self._create_task(status=STATUS_PENDING_VERIFICATION)
        mock_llm = {"success": True, "content": "PASS - Implementation looks good."}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.stage_gate_tool.append_event'):
                result = run_qa_gate(task["id"])

        assert result["gate"] == "qa"
        assert result["passed"] is True
        assert result["status"] == STATUS_TESTING_PASSED

        updated = bootstrap.get_task(task["id"])
        assert updated["status"] == STATUS_TESTING_PASSED

    def test_qa_fail(self):
        task = self._create_task(status=STATUS_PENDING_VERIFICATION)
        mock_llm = {"success": True, "content": "FAIL - Missing error handling."}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.stage_gate_tool.append_event'):
                result = run_qa_gate(task["id"])

        assert result["gate"] == "qa"
        assert result["passed"] is False
        assert result["status"] == STATUS_REJECTED

        updated = bootstrap.get_task(task["id"])
        assert updated["status"] == STATUS_REJECTED

    def test_qa_llm_failure(self):
        task = self._create_task(status=STATUS_PENDING_VERIFICATION)
        mock_llm = {"success": False, "error": "LLM unavailable", "content": None}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.stage_gate_tool.append_event'):
                result = run_qa_gate(task["id"])

        assert result["passed"] is False
        assert result["status"] == STATUS_REJECTED

    def test_qa_nonexistent_task(self):
        result = run_qa_gate(999)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_qa_empty_content(self):
        task = self._create_task(status=STATUS_PENDING_VERIFICATION)
        mock_llm = {"success": True, "content": ""}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.stage_gate_tool.append_event'):
                result = run_qa_gate(task["id"])

        assert result["passed"] is False


# ── Architect Gate ───────────────────────────────────────────────


class TestRunArchitectGate(_TestSetup):
    def test_architect_approve(self):
        task = self._create_task(status=STATUS_TESTING_PASSED)
        mock_llm = {"success": True, "content": "APPROVE - Architecture conforms."}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.orchestrator_tool.load_role', return_value={
                "system_prompt": "You are an architect."
            }):
                with patch('tools.stage_gate_tool.append_event'):
                    result = run_architect_gate(task["id"])

        assert result["gate"] == "architect"
        assert result["passed"] is True
        assert result["status"] == STATUS_COMPLETED

        updated = bootstrap.get_task(task["id"])
        assert updated["status"] == STATUS_COMPLETED

    def test_architect_reject(self):
        task = self._create_task(status=STATUS_TESTING_PASSED)
        mock_llm = {"success": True, "content": "REJECT - Violates separation of concerns."}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.orchestrator_tool.load_role', return_value={
                "system_prompt": "You are an architect."
            }):
                with patch('tools.stage_gate_tool.append_event'):
                    result = run_architect_gate(task["id"])

        assert result["passed"] is False
        assert result["status"] == STATUS_REJECTED

    def test_architect_nonexistent_task(self):
        result = run_architect_gate(999)
        assert result["success"] is False

    def test_architect_no_qa_feedback(self):
        task = self._create_task(status=STATUS_TESTING_PASSED)
        mock_llm = {"success": True, "content": "APPROVE - Good."}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.orchestrator_tool.load_role', return_value={
                "system_prompt": "You are an architect."
            }):
                with patch('tools.stage_gate_tool.append_event'):
                    result = run_architect_gate(task["id"])

        assert result["passed"] is True


# ── Contract Gate ────────────────────────────────────────────────


class TestRunContractGate(_TestSetup):
    def test_no_spec_passes_by_default(self):
        task = self._create_task()
        result = run_contract_gate(task["id"])
        assert result["passed"] is True
        assert "No interface specification" in result["feedback"]

    def test_contract_with_spec(self):
        task = self._create_task()
        task["interface_spec_id"] = "spec-1"
        bootstrap.update_task_status(task["id"], task["status"])

        with patch('tools.stage_gate_tool.append_event'):
            result = run_contract_gate(task["id"])
        assert result["passed"] is True

    def test_contract_nonexistent_task(self):
        result = run_contract_gate(999)
        assert result["success"] is False


# ── Reject Task ──────────────────────────────────────────────────


class TestRejectTask(_TestSetup):
    def test_reject_adds_artifact(self):
        task = self._create_task()
        reject_task(task["id"], "qa", "Missing test coverage")

        updated = bootstrap.get_task(task["id"])
        assert updated["status"] == STATUS_REJECTED
        assert len(updated["verification_artifacts"]) == 1
        assert updated["verification_artifacts"][0]["gate"] == "qa"
        assert updated["verification_artifacts"][0]["result"] == "failed"

    def test_reject_appends_multiple_artifacts(self):
        task = self._create_task()
        reject_task(task["id"], "qa", "First fail")
        reject_task(task["id"], "architect", "Second fail")

        updated = bootstrap.get_task(task["id"])
        assert len(updated["verification_artifacts"]) == 2

    def test_reject_nonexistent_task_does_nothing(self):
        reject_task(999, "qa", "reason")


# ── Full Pipeline ────────────────────────────────────────────────


class TestRunFullPipeline(_TestSetup):
    def test_full_pipeline_success(self):
        task = self._create_task(status="in_progress")
        mock_qa = {"success": True, "content": "PASS - Good implementation."}
        mock_arch = {"success": True, "content": "APPROVE - Clean architecture."}

        with patch('tools.stage_gate_tool.query_llm', side_effect=[mock_qa, mock_arch]):
            with patch('tools.orchestrator_tool.load_role', return_value={
                "system_prompt": "You are an architect."
            }):
                with patch('tools.stage_gate_tool.append_event'):
                    result = run_full_pipeline(task["id"], "Done and ready")

        assert result["success"] is True
        assert len(result["results"]) == 4
        assert result["stopped_at"] is None

        updated = bootstrap.get_task(task["id"])
        assert updated["status"] == STATUS_COMPLETED

    def test_full_pipeline_fails_at_qa(self):
        task = self._create_task(status="in_progress")
        mock_qa = {"success": True, "content": "FAIL - Bug found."}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_qa):
            with patch('tools.stage_gate_tool.append_event'):
                result = run_full_pipeline(task["id"], "Done")

        assert result["success"] is False
        assert result["stopped_at"] == "qa"

    def test_full_pipeline_nonexistent(self):
        result = run_full_pipeline(999, "notes")
        assert result["success"] is False
        assert "not found" in result["error"]


# ── Pipeline Status ──────────────────────────────────────────────


class TestGetPipelineStatus(_TestSetup):
    def test_status_pending(self):
        task = self._create_task(status="pending")
        result = get_pipeline_status(task["id"])
        assert result["success"] is True
        assert result["status"] == "pending"
        assert result["gates_passed"] == []

    def test_status_qa_passed(self):
        task = self._create_task(status=STATUS_TESTING_PASSED)
        result = get_pipeline_status(task["id"])
        assert "qa" in result["gates_passed"]

    def test_status_completed(self):
        task = self._create_task(status=STATUS_COMPLETED)
        result = get_pipeline_status(task["id"])
        assert "qa" in result["gates_passed"]

    def test_status_with_failed_artifacts(self):
        task = self._create_task(status=STATUS_REJECTED)
        reject_task(task["id"], "qa", "Failed QA")

        result = get_pipeline_status(task["id"])
        assert "qa" in result["gates_failed"]

    def test_nonexistent_task(self):
        result = get_pipeline_status(999)
        assert result["success"] is False


# ── Kanban Sync Integration ──────────────────────────────────────


class TestKanbanSync(_TestSetup):
    def test_submit_syncs_kanban_column(self):
        task = self._create_task()
        submit_for_verification(task["id"], "Ready")

        state = bootstrap.load_project_state()
        kanban = state.get("kanban", {})
        assert task["id"] in kanban.get("testing", [])

    def test_qa_pass_syncs_kanban(self):
        task = self._create_task(status=STATUS_PENDING_VERIFICATION)
        mock_llm = {"success": True, "content": "PASS - OK."}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.stage_gate_tool.append_event'):
                run_qa_gate(task["id"])

        state = bootstrap.load_project_state()
        kanban = state.get("kanban", {})
        assert task["id"] in kanban.get("architect_review", [])

    def test_architect_approve_syncs_kanban(self):
        task = self._create_task(status=STATUS_TESTING_PASSED)
        mock_llm = {"success": True, "content": "APPROVE - Good."}

        with patch('tools.stage_gate_tool.query_llm', return_value=mock_llm):
            with patch('tools.orchestrator_tool.load_role', return_value={
                "system_prompt": "You are an architect."
            }):
                with patch('tools.stage_gate_tool.append_event'):
                    run_architect_gate(task["id"])

        state = bootstrap.load_project_state()
        kanban = state.get("kanban", {})
        assert task["id"] in kanban.get("done", [])

    def test_reject_syncs_kanban(self):
        task = self._create_task()
        reject_task(task["id"], "qa", "Failed")

        state = bootstrap.load_project_state()
        kanban = state.get("kanban", {})
        # Rejected work is surfaced in the blocked column, not hidden as
        # fresh backlog.
        assert task["id"] in kanban.get("blocked", [])
