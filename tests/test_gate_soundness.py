"""Phase-1 regression tests: the Definition of Done must be trustworthy.

Covers:
- fail-closed verdict parsing (the substring bug that passed rejections),
- real test execution inside the QA gate,
- the delegate_task retry loop and blocked_needs_human circuit breaker,
- the kanban drag-to-done bypass,
- registry_tool/bootstrap update consolidation (board stays in sync).
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap
from tools.stage_gate_tool import (
    parse_gate_verdict,
    run_automated_tests,
    run_qa_gate,
    run_full_pipeline,
    QA_PASS_TOKENS,
    QA_FAIL_TOKENS,
    ARCH_PASS_TOKENS,
    ARCH_FAIL_TOKENS,
)


# ── Verdict parsing ─────────────────────────────────────────────


class TestParseGateVerdict:
    def _qa(self, content):
        return parse_gate_verdict(content, QA_PASS_TOKENS, QA_FAIL_TOKENS)

    def _arch(self, content):
        return parse_gate_verdict(content, ARCH_PASS_TOKENS, ARCH_FAIL_TOKENS)

    def test_plain_pass(self):
        assert self._qa("PASS - implementation looks correct") == (True, True)

    def test_plain_fail(self):
        assert self._qa("FAIL - missing error handling") == (False, True)

    def test_regression_fail_containing_pass(self):
        # The original bug: "PASS" appears inside the rejection text.
        passed, matched = self._qa("FAIL - the tests do not pass the acceptance criteria")
        assert passed is False and matched is True

    def test_regression_cannot_approve(self):
        # Prose with no leading verdict must fail closed, not match "APPROVE".
        passed, matched = self._arch("I cannot approve this change because it breaks layering.")
        assert passed is False

    def test_regression_not_approved(self):
        passed, matched = self._arch("NOT APPROVED - violates the interface spec")
        assert passed is False and matched is True

    def test_markdown_decorated_pass(self):
        assert self._arch("**APPROVE** - consistent with the architecture")[0] is True

    def test_verdict_prefix(self):
        assert self._qa("Verdict: PASS - all criteria met")[0] is True

    def test_prose_only_fails_closed(self):
        passed, matched = self._qa("The implementation generally looks fine to me.")
        assert passed is False and matched is False

    def test_empty_fails_closed(self):
        assert self._qa("") == (False, False)
        assert self._qa(None) == (False, False)

    def test_reject_verdict(self):
        assert self._arch("REJECT - introduces coupling to the UI layer")[0] is False

    def test_compliance_variants(self):
        tokens = dict(pass_tokens=("COMPLIANT",), fail_tokens=("NON_COMPLIANT", "NONCOMPLIANT", "NON"))
        assert parse_gate_verdict("COMPLIANT - matches the contract", **tokens)[0] is True
        assert parse_gate_verdict("NON_COMPLIANT - missing endpoint", **tokens)[0] is False
        # Hyphen/space variants previously false-passed the substring check.
        assert parse_gate_verdict("NON-COMPLIANT - missing endpoint", **tokens)[0] is False
        assert parse_gate_verdict("NOT COMPLIANT - schema mismatch", **tokens)[0] is False


# ── Automated tests inside the QA gate ──────────────────────────


def _make_project(tmp_path, passing=True):
    proj = tmp_path / "target_project"
    (proj / "tests").mkdir(parents=True)
    body = "def test_it():\n    assert True\n" if passing else "def test_it():\n    assert False\n"
    (proj / "tests" / "test_it.py").write_text(body, encoding="utf-8")
    return proj


class TestRunAutomatedTests:
    def test_passing_project(self, tmp_path):
        proj = _make_project(tmp_path, passing=True)
        result = run_automated_tests({"project_path": str(proj)})
        assert result["ran"] is True
        assert result["passed"] is True
        assert result["returncode"] == 0

    def test_failing_project(self, tmp_path):
        proj = _make_project(tmp_path, passing=False)
        result = run_automated_tests({"project_path": str(proj)})
        assert result["ran"] is True
        assert result["passed"] is False

    def test_no_project_dir(self):
        result = run_automated_tests({})
        assert result["ran"] is False

    def test_no_tests_found(self, tmp_path):
        proj = tmp_path / "empty_project"
        proj.mkdir()
        result = run_automated_tests({"project_path": str(proj)})
        assert result["ran"] is False
        assert "no automated tests" in result["reason"]


class TestQAGateRunsTests:
    def test_failing_tests_reject_without_llm(self, tmp_path):
        proj = _make_project(tmp_path, passing=False)
        task = bootstrap.add_task("Implement feature", agent="developer")
        bootstrap.update_task_fields(task["id"], {"project_path": str(proj)})

        llm = MagicMock()
        with patch("tools.stage_gate_tool.query_llm", llm):
            result = run_qa_gate(task["id"])

        assert result["passed"] is False
        llm.assert_not_called()  # deterministic failure needs no opinion
        refreshed = bootstrap.get_task(task["id"])
        assert refreshed["status"] == "rejected"
        kinds = [a.get("kind") for a in refreshed["verification_artifacts"] if isinstance(a, dict)]
        assert "automated_tests" in kinds

    def test_passing_tests_feed_llm_review(self, tmp_path):
        proj = _make_project(tmp_path, passing=True)
        task = bootstrap.add_task("Implement feature", agent="developer")
        bootstrap.update_task_fields(task["id"], {"project_path": str(proj)})

        llm = MagicMock(return_value={"success": True, "content": "PASS - solid work"})
        with patch("tools.stage_gate_tool.query_llm", llm):
            result = run_qa_gate(task["id"])

        assert result["passed"] is True
        llm.assert_called_once()
        prompt = llm.call_args[0][0]
        assert "Automated tests PASSED" in prompt
        assert bootstrap.get_task(task["id"])["status"] == "testing_passed"

    def test_unparseable_verdict_fails_closed(self):
        task = bootstrap.add_task("Implement feature", agent="developer")
        llm = MagicMock(return_value={"success": True, "content": "Looks reasonable overall."})
        with patch("tools.stage_gate_tool.query_llm", llm):
            result = run_qa_gate(task["id"])
        assert result["passed"] is False
        assert bootstrap.get_task(task["id"])["status"] == "rejected"


# ── Retry loop and circuit breaker ──────────────────────────────


class TestDelegateCircuitBreaker:
    def _delegate(self, gate_llm):
        from tools import orchestrator_tool
        exec_llm = MagicMock(return_value={"success": True, "content": "work output"})
        with patch("tools.orchestrator_tool.query_llm", exec_llm), \
             patch("tools.orchestrator_tool.store_insight"), \
             patch("tools.stage_gate_tool.query_llm", gate_llm):
            return orchestrator_tool.delegate_task(
                "Research a topic", "researcher", run_stage_gates=True,
            ), exec_llm

    def test_repeated_rejection_blocks_for_human(self):
        from tools.clarification_tool import list_requests
        gate_llm = MagicMock(return_value={"success": True, "content": "FAIL - wrong approach"})

        result, exec_llm = self._delegate(gate_llm)

        assert result["success"] is False
        assert result.get("blocked") is True
        task = bootstrap.get_task(result["task_id"])
        assert task["status"] == "blocked_needs_human"
        assert task["retry_count"] == 2  # two persisted retries after three attempts
        assert exec_llm.call_count == 3  # initial + 2 retries

        pending = list_requests(status="pending")
        assert len(pending) == 1
        assert f"Task #{task['id']}" in pending[0]["question"]

        # Blocked work is visible on the board, not hidden in backlog.
        kanban = bootstrap.load_project_state()["kanban"]
        assert task["id"] in kanban.get("blocked", [])

    def test_rejection_then_recovery(self):
        gate_llm = MagicMock(side_effect=[
            {"success": True, "content": "FAIL - missing edge cases"},   # attempt 1: QA
            {"success": True, "content": "PASS - fixed"},                # attempt 2: QA
            {"success": True, "content": "APPROVE - consistent"},        # attempt 2: architect
        ])

        result, exec_llm = self._delegate(gate_llm)

        assert result["success"] is True
        task = bootstrap.get_task(result["task_id"])
        assert task["status"] == "done"
        assert task["retry_count"] == 1
        # The architect's signoff note survives (no post-gate clobber).
        assert task["completion_notes"].startswith("Architect approved")
        # The retry prompt carried the rejection feedback forward.
        second_prompt = exec_llm.call_args_list[1][0][0]
        assert "missing edge cases" in second_prompt

    def test_gates_pass_first_time(self):
        gate_llm = MagicMock(side_effect=[
            {"success": True, "content": "PASS - good"},
            {"success": True, "content": "APPROVE - good"},
        ])
        result, exec_llm = self._delegate(gate_llm)
        assert result["success"] is True
        assert exec_llm.call_count == 1
        assert bootstrap.get_task(result["task_id"])["retry_count"] == 0


# ── Kanban done-guard ───────────────────────────────────────────


class TestTransferTaskGuard:
    def test_cannot_drag_ungated_task_to_done(self):
        from tools.kanban_tool import transfer_task
        task = bootstrap.add_task("Some work", agent="developer")
        assert transfer_task(task["id"], "backlog", "done") is False
        assert bootstrap.get_task(task["id"])["status"] == "pending"

    def test_gated_task_can_move_to_done(self):
        from tools.kanban_tool import transfer_task
        task = bootstrap.add_task("Some work", agent="developer")
        bootstrap.update_task_status(task["id"], "done")
        assert transfer_task(task["id"], "architect_review", "done") is True


# ── Registry consolidation ──────────────────────────────────────


class TestRegistryConsolidation:
    def test_registry_tool_update_syncs_kanban(self):
        from tools.registry_tool import update_task_status as registry_update
        task = bootstrap.add_task("Some work", agent="developer")

        assert registry_update(task["id"], "in_progress") is True

        kanban = bootstrap.load_project_state()["kanban"]
        assert task["id"] in kanban["in_progress"]
        # Per-task file mirrors the registry now.
        import json
        task_file = bootstrap.TASKS_DIR / f"task_{task['id']}.json"
        assert json.loads(task_file.read_text())["status"] == "in_progress"

    def test_update_task_fields_persists(self):
        task = bootstrap.add_task("Some work", agent="developer")
        bootstrap.update_task_fields(task["id"], {"retry_count": 5})
        assert bootstrap.get_task(task["id"])["retry_count"] == 5
