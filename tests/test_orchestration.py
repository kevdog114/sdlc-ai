"""Tests for the Multi-Agent Orchestration Framework.

Covers role definitions, goal decomposition, task delegation, monitoring,
failure handling, and end-to-end orchestration with recovery scenarios.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.orchestrator_tool import (
    list_roles,
    load_role,
    decompose_goal,
    delegate_task,
    monitor_tasks,
    get_failed_tasks,
    handle_failure,
    run_orchestration,
    TASK_STATUS_PENDING,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_ESCALATED,
    ROLES_DIR,
    MAX_RETRIES,
)


# ── Role Definitions ────────────────────────────────────────────


class TestListRoles:
    def test_returns_existing_roles(self):
        roles = list_roles()
        assert "architect" in roles
        assert "developer" in roles
        assert "qa" in roles
        assert "researcher" in roles

    def test_returns_sorted_list(self):
        roles = list_roles()
        assert roles == sorted(roles)

    def test_returns_only_json_stems(self):
        roles = list_roles()
        for r in roles:
            assert (ROLES_DIR / f"{r}.json").is_file()


class TestLoadRole:
    def test_load_architect(self):
        role = load_role("architect")
        assert role is not None
        assert role["name"] == "architect"
        assert "system_prompt" in role
        assert "goal_decomposition" in role["capabilities"]

    def test_load_developer(self):
        role = load_role("developer")
        assert role is not None
        assert role["name"] == "developer"
        assert "code_implementation" in role["capabilities"]

    def test_load_qa(self):
        role = load_role("qa")
        assert role is not None
        assert role["name"] == "qa"
        assert "test_writing" in role["capabilities"]

    def test_load_researcher(self):
        role = load_role("researcher")
        assert role is not None
        assert role["name"] == "researcher"
        assert "information_gathering" in role["capabilities"]

    def test_load_nonexistent_role(self):
        assert load_role("nonexistent_role") is None

    def test_role_has_required_fields(self):
        for rname in list_roles():
            role = load_role(rname)
            assert role is not None
            assert "name" in role
            assert "system_prompt" in role
            assert "preferred_model" in role
            assert "capabilities" in role
            assert isinstance(role["capabilities"], list)
            assert len(role["capabilities"]) > 0


# ── Goal Decomposition ──────────────────────────────────────────


class TestDecomposeGoal:
    def test_decompose_returns_valid_structure(self):
        mock_response = {
            "success": True,
            "content": json.dumps([
                {"task": "Create module skeleton", "role": "developer", "dependencies": []},
                {"task": "Write tests", "role": "qa", "dependencies": [0]},
            ]),
        }
        with patch('tools.orchestrator_tool.query_llm', return_value=mock_response):
            with patch('tools.orchestrator_tool.append_event'):
                result = decompose_goal("Build a new feature")
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["task"] == "Create module skeleton"
        assert result[0]["role"] == "developer"
        assert result[1]["dependencies"] == [0]

    def test_decompose_uses_llm_with_correct_prompt(self):
        mock_response = {
            "success": True,
            "content": json.dumps([
                {"task": "Single task", "role": "developer", "dependencies": []},
            ]),
        }
        with patch('tools.orchestrator_tool.query_llm', return_value=mock_response) as mock_q:
            with patch('tools.orchestrator_tool.append_event'):
                decompose_goal("Test goal")
        call_args = mock_q.call_args
        prompt = call_args[0][0]
        assert "Test goal" in prompt
        assert "architect" in prompt or "developer" in prompt

    def test_decompose_fallback_on_llm_failure(self):
        mock_response = {"success": False, "error": "LLM unavailable"}
        with patch('tools.orchestrator_tool.query_llm', return_value=mock_response):
            with patch('tools.orchestrator_tool.append_event'):
                result = decompose_goal("Build something")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["task"] == "Build something"
        assert result[0]["role"] == "developer"

    def test_decompose_fallback_on_invalid_json(self):
        mock_response = {"success": True, "content": "not valid json"}
        with patch('tools.orchestrator_tool.query_llm', return_value=mock_response):
            with patch('tools.orchestrator_tool.append_event'):
                result = decompose_goal("Build something")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["task"] == "Build something"

    def test_decompose_fallback_on_malformed_tasks(self):
        mock_response = {"success": True, "content": json.dumps([{"invalid": "structure"}])}
        with patch('tools.orchestrator_tool.query_llm', return_value=mock_response):
            with patch('tools.orchestrator_tool.append_event'):
                result = decompose_goal("Build something")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["task"] == "Build something"

    def test_decompose_logs_event(self):
        mock_response = {
            "success": True,
            "content": json.dumps([
                {"task": "t1", "role": "developer", "dependencies": []},
            ]),
        }
        with patch('tools.orchestrator_tool.query_llm', return_value=mock_response):
            with patch('tools.orchestrator_tool.append_event') as mock_evt:
                decompose_goal("Test goal")
        mock_evt.assert_called_once()
        assert mock_evt.call_args[0][0] == "tool:orchestrator"
        assert mock_evt.call_args[0][1]["action"] == "decompose_goal"


# ── Task Delegation ─────────────────────────────────────────────


class TestDelegateTask:
    def test_delegate_success(self):
        mock_task = {"id": 100, "description": "test task", "status": "pending", "agent": "developer"}
        mock_llm = {"success": True, "content": "Implementation done."}

        with patch('tools.orchestrator_tool.create_new_task', return_value=mock_task):
            with patch('tools.orchestrator_tool.registry_update') as mock_upd:
                with patch('tools.orchestrator_tool.query_llm', return_value=mock_llm):
                    with patch('tools.orchestrator_tool.store_insight'):
                        with patch('tools.orchestrator_tool.append_event'):
                            result = delegate_task("test task", "developer")

        assert result["success"] is True
        assert result["task_id"] == 100
        assert result["output"] == "Implementation done."
        assert result["error"] is None

        status_calls = [c for c in mock_upd.call_args_list]
        assert status_calls[0][0] == (100, TASK_STATUS_IN_PROGRESS)
        assert status_calls[1][0][0] == 100
        assert status_calls[1][0][1] == TASK_STATUS_DONE

    def test_delegate_failure(self):
        mock_task = {"id": 101, "description": "failing task", "status": "pending", "agent": "developer"}
        mock_llm = {"success": False, "error": "Connection timeout"}

        with patch('tools.orchestrator_tool.create_new_task', return_value=mock_task):
            with patch('tools.orchestrator_tool.registry_update') as mock_upd:
                with patch('tools.orchestrator_tool.query_llm', return_value=mock_llm):
                    with patch('tools.orchestrator_tool.append_event'):
                        result = delegate_task("failing task", "developer")

        assert result["success"] is False
        assert result["task_id"] == 101
        assert result["error"] == "Connection timeout"

        status_calls = [c for c in mock_upd.call_args_list]
        assert status_calls[1][0][1] == TASK_STATUS_FAILED

    def test_delegate_unknown_role(self):
        with patch('tools.orchestrator_tool.append_event'):
            result = delegate_task("some task", "unknown_role")
        assert result["success"] is False
        assert "Unknown role" in result["error"]

    def test_delegate_with_context(self):
        mock_task = {"id": 102, "description": "task with context", "status": "pending"}
        mock_llm = {"success": True, "content": "Done with context."}

        with patch('tools.orchestrator_tool.create_new_task', return_value=mock_task):
            with patch('tools.orchestrator_tool.registry_update'):
                with patch('tools.orchestrator_tool.query_llm', return_value=mock_llm) as mock_q:
                    with patch('tools.orchestrator_tool.store_insight'):
                        with patch('tools.orchestrator_tool.append_event'):
                            delegate_task("task with context", "developer", context="Extra info")

        user_prompt = mock_q.call_args[0][0]
        assert "Extra info" in user_prompt

    def test_delegate_stores_insight_on_success(self):
        mock_task = {"id": 103, "description": "insight task", "status": "pending"}
        mock_llm = {"success": True, "content": "Result stored."}

        with patch('tools.orchestrator_tool.create_new_task', return_value=mock_task):
            with patch('tools.orchestrator_tool.registry_update'):
                with patch('tools.orchestrator_tool.query_llm', return_value=mock_llm):
                    with patch('tools.orchestrator_tool.store_insight') as mock_store:
                        with patch('tools.orchestrator_tool.append_event'):
                            delegate_task("insight task", "developer")

        mock_store.assert_called_once()
        assert "insight task" in mock_store.call_args[0][1]


# ── Monitoring ──────────────────────────────────────────────────


class TestMonitorTasks:
    def test_groups_tasks_by_status(self):
        mock_tasks = [
            {"id": 1, "status": "pending", "description": "a"},
            {"id": 2, "status": "in_progress", "description": "b"},
            {"id": 3, "status": "done", "description": "c"},
            {"id": 4, "status": "done", "description": "d"},
            {"id": 5, "status": "failed", "description": "e"},
        ]
        with patch('tools.orchestrator_tool.list_tasks', return_value=mock_tasks):
            with patch('tools.orchestrator_tool.append_event'):
                result = monitor_tasks()

        assert "pending" in result
        assert len(result["pending"]) == 1
        assert len(result["done"]) == 2
        assert len(result["failed"]) == 1

    def test_empty_registry(self):
        with patch('tools.orchestrator_tool.list_tasks', return_value=[]):
            with patch('tools.orchestrator_tool.append_event'):
                result = monitor_tasks()
        assert result == {}

    def test_monitor_logs_event(self):
        with patch('tools.orchestrator_tool.list_tasks', return_value=[]):
            with patch('tools.orchestrator_tool.append_event') as mock_evt:
                monitor_tasks()
        mock_evt.assert_called_once()
        assert mock_evt.call_args[0][1]["action"] == "monitor_tasks"


class TestGetFailedTasks:
    def test_returns_only_failed(self):
        mock_tasks = [
            {"id": 1, "status": "failed", "description": "f1"},
            {"id": 2, "status": "failed", "description": "f2"},
            {"id": 3, "status": "done", "description": "ok"},
        ]
        with patch('tools.orchestrator_tool.list_tasks', return_value=mock_tasks):
            result = get_failed_tasks()
        assert len(result) == 2
        assert all(t["status"] == "failed" for t in result)

    def test_no_failed_tasks(self):
        with patch('tools.orchestrator_tool.list_tasks', return_value=[]):
            result = get_failed_tasks()
        assert result == []


# ── Failure Handling ────────────────────────────────────────────


class TestHandleFailure:
    def test_retry_on_first_failure(self):
        task = {
            "id": 200,
            "description": "retryable task",
            "agent": "developer",
            "retry_count": 0,
            "status": "failed",
        }
        mock_result = {"task_id": 201, "success": True, "output": "Fixed!", "error": None}

        with patch('tools.orchestrator_tool.registry_update'):
            with patch('tools.orchestrator_tool.delegate_task', return_value=mock_result):
                with patch('tools.orchestrator_tool.append_event'):
                    result = handle_failure(task, "original error")

        assert result["action"] == "retry"
        assert result["success"] is True
        assert result["output"] == "Fixed!"
        assert task["retry_count"] == 1

    def test_retry_on_second_failure(self):
        task = {
            "id": 201,
            "description": "retryable task 2",
            "agent": "developer",
            "retry_count": 1,
            "status": "failed",
        }
        mock_result = {"task_id": 202, "success": True, "output": "Fixed on retry 2.", "error": None}

        with patch('tools.orchestrator_tool.registry_update'):
            with patch('tools.orchestrator_tool.delegate_task', return_value=mock_result):
                with patch('tools.orchestrator_tool.append_event'):
                    result = handle_failure(task, "error again")

        assert result["action"] == "retry"
        assert result["success"] is True
        assert task["retry_count"] == 2

    def test_reassign_after_max_retries(self):
        task = {
            "id": 202,
            "description": "needs reassign",
            "agent": "developer",
            "retry_count": MAX_RETRIES,
            "status": "failed",
        }
        mock_result = {"task_id": 203, "success": True, "output": "Fixed by QA.", "error": None}

        with patch('tools.orchestrator_tool.registry_update'):
            with patch('tools.orchestrator_tool.delegate_task', return_value=mock_result):
                with patch('tools.orchestrator_tool.list_roles', return_value=["developer", "qa", "architect"]):
                    with patch('tools.orchestrator_tool.append_event'):
                        result = handle_failure(task, "max retries exceeded")

        assert result["action"] == "re-assign"
        assert result["success"] is True

    def test_escalate_when_reassign_fails(self):
        task = {
            "id": 203,
            "description": "escalate task",
            "agent": "developer",
            "retry_count": MAX_RETRIES,
            "status": "failed",
        }
        mock_result = {"task_id": 204, "success": False, "output": "", "error": "Still broken"}

        with patch('tools.orchestrator_tool.registry_update'):
            with patch('tools.orchestrator_tool.delegate_task', return_value=mock_result):
                with patch('tools.orchestrator_tool.list_roles', return_value=["developer", "qa"]):
                    with patch('tools.orchestrator_tool.store_insight'):
                        with patch('tools.orchestrator_tool.append_event'):
                            result = handle_failure(task, "unfixable")

        assert result["action"] == "escalate"
        assert result["success"] is False
        assert "Escalated" in result["error"]

    def test_escalate_when_no_alternative_roles(self):
        task = {
            "id": 204,
            "description": "no alt roles",
            "agent": "developer",
            "retry_count": MAX_RETRIES,
            "status": "failed",
        }

        with patch('tools.orchestrator_tool.registry_update'):
            with patch('tools.orchestrator_tool.list_roles', return_value=["developer"]):
                with patch('tools.orchestrator_tool.store_insight'):
                    with patch('tools.orchestrator_tool.append_event'):
                        result = handle_failure(task, "no one else can do it")

        assert result["action"] == "escalate"
        assert result["success"] is False


# ── End-to-End Orchestration ────────────────────────────────────


class TestRunOrchestration:
    def test_full_successful_orchestration(self):
        mock_decomposition = [
            {"task": "Create module", "role": "developer", "dependencies": []},
            {"task": "Write tests", "role": "qa", "dependencies": [0]},
        ]
        mock_delegations = iter([
            {"task_id": 300, "success": True, "output": "Module created.", "error": None},
            {"task_id": 301, "success": True, "output": "Tests written.", "error": None},
        ])

        def side_effect_delegate(*args, **kwargs):
            return next(mock_delegations)

        with patch('tools.orchestrator_tool.decompose_goal', return_value=mock_decomposition):
            with patch('tools.orchestrator_tool.delegate_task', side_effect=side_effect_delegate):
                with patch('tools.orchestrator_tool.store_insight'):
                    with patch('tools.orchestrator_tool.append_event'):
                        result = run_orchestration("Build feature X")

        assert result["goal"] == "Build feature X"
        assert len(result["results"]) == 2
        assert result["summary"]["total"] == 2
        assert result["summary"]["succeeded"] == 2
        assert result["summary"]["failed"] == 0

    def test_orchestration_with_failure_recovery(self):
        mock_decomposition = [
            {"task": "Create module", "role": "developer", "dependencies": []},
            {"task": "Write tests", "role": "qa", "dependencies": [0]},
        ]

        mock_task_obj = {
            "id": 301,
            "description": "Write tests",
            "agent": "qa",
            "retry_count": 0,
            "status": "failed",
        }

        mock_delegations = iter([
            {"task_id": 300, "success": True, "output": "Module created.", "error": None},
            {"task_id": 301, "success": False, "output": "", "error": "LLM timeout"},
        ])
        mock_recovery = {
            "action": "retry",
            "task_id": 301,
            "success": True,
            "output": "Tests fixed on retry.",
            "error": None,
        }

        def side_effect_delegate(*args, **kwargs):
            return next(mock_delegations)

        with patch('tools.orchestrator_tool.decompose_goal', return_value=mock_decomposition):
            with patch('tools.orchestrator_tool.delegate_task', side_effect=side_effect_delegate):
                with patch('tools.orchestrator_tool.get_task_by_id', return_value=mock_task_obj):
                    with patch('tools.orchestrator_tool.handle_failure', return_value=mock_recovery):
                        with patch('tools.orchestrator_tool.store_insight'):
                            with patch('tools.orchestrator_tool.append_event'):
                                result = run_orchestration("Build with recovery")

        assert result["summary"]["total"] == 2
        assert result["summary"]["succeeded"] == 2
        assert result["results"][1]["recovery_action"] == "retry"

    def test_orchestration_with_unrecoverable_failure(self):
        mock_decomposition = [
            {"task": "Impossible task", "role": "developer", "dependencies": []},
        ]

        mock_task_obj = {
            "id": 302,
            "description": "Impossible task",
            "agent": "developer",
            "retry_count": 0,
            "status": "failed",
        }

        mock_delegations = iter([
            {"task_id": 302, "success": False, "output": "", "error": "Cannot complete"},
        ])
        mock_recovery = {
            "action": "escalate",
            "task_id": 302,
            "success": False,
            "output": "",
            "error": "Escalated: Cannot complete",
        }

        def side_effect_delegate(*args, **kwargs):
            return next(mock_delegations)

        with patch('tools.orchestrator_tool.decompose_goal', return_value=mock_decomposition):
            with patch('tools.orchestrator_tool.delegate_task', side_effect=side_effect_delegate):
                with patch('tools.orchestrator_tool.get_task_by_id', return_value=mock_task_obj):
                    with patch('tools.orchestrator_tool.handle_failure', return_value=mock_recovery):
                        with patch('tools.orchestrator_tool.store_insight'):
                            with patch('tools.orchestrator_tool.append_event'):
                                result = run_orchestration("Impossible goal")

        assert result["summary"]["total"] == 1
        assert result["summary"]["succeeded"] == 0
        assert result["summary"]["failed"] == 1

    def test_orchestration_stores_summary_insight(self):
        mock_decomposition = [
            {"task": "t1", "role": "developer", "dependencies": []},
        ]
        mock_result = {"task_id": 303, "success": True, "output": "OK", "error": None}

        with patch('tools.orchestrator_tool.decompose_goal', return_value=mock_decomposition):
            with patch('tools.orchestrator_tool.delegate_task', return_value=mock_result):
                with patch('tools.orchestrator_tool.store_insight') as mock_store:
                    with patch('tools.orchestrator_tool.append_event'):
                        run_orchestration("Summary test goal")

        mock_store.assert_called()
        call_args = mock_store.call_args
        assert "Summary test goal" in call_args[0][1]

    def test_orchestration_logs_start_and_complete_events(self):
        mock_decomposition = [
            {"task": "t1", "role": "developer", "dependencies": []},
        ]
        mock_result = {"task_id": 304, "success": True, "output": "OK", "error": None}

        with patch('tools.orchestrator_tool.decompose_goal', return_value=mock_decomposition):
            with patch('tools.orchestrator_tool.delegate_task', return_value=mock_result):
                with patch('tools.orchestrator_tool.store_insight'):
                    with patch('tools.orchestrator_tool.append_event') as mock_evt:
                        run_orchestration("Event test")

        events = [c[0][1] for c in mock_evt.call_args_list]
        actions = [e["action"] for e in events]
        assert "orchestration_start" in actions
        assert "orchestration_complete" in actions


# ── Failure Recovery Scenarios ──────────────────────────────────


class TestFailureRecovery:
    def test_retry_then_succeed(self):
        task = {
            "id": 400,
            "description": "flaky task",
            "agent": "developer",
            "retry_count": 0,
            "status": "failed",
        }
        mock_result = {"task_id": 401, "success": True, "output": "Success on retry!", "error": None}

        with patch('tools.orchestrator_tool.registry_update'):
            with patch('tools.orchestrator_tool.delegate_task', return_value=mock_result):
                with patch('tools.orchestrator_tool.append_event'):
                    result = handle_failure(task, "flaky error")

        assert result["action"] == "retry"
        assert result["success"] is True

    def test_retry_twice_then_reassign_then_succeed(self):
        task = {
            "id": 401,
            "description": "stubborn task",
            "agent": "developer",
            "retry_count": 1,
            "status": "failed",
        }

        def delegate_side_effect(*args, **kwargs):
            role = args[1] if len(args) > 1 else kwargs.get("role_name", "developer")
            if role == "developer":
                return {"task_id": 402, "success": True, "output": "Fixed by dev retry.", "error": None}
            return {"task_id": 403, "success": True, "output": "Fixed by QA.", "error": None}

        with patch('tools.orchestrator_tool.registry_update'):
            with patch('tools.orchestrator_tool.delegate_task', side_effect=delegate_side_effect):
                with patch('tools.orchestrator_tool.list_roles', return_value=["developer", "qa"]):
                    with patch('tools.orchestrator_tool.append_event'):
                        r1 = handle_failure(task, "still broken")
                        assert r1["action"] == "retry"
                        assert task["retry_count"] == 2

                        task["status"] = "failed"
                        r2 = handle_failure(task, "still broken again")
                        assert r2["action"] == "re-assign"

    def test_full_escalation_path(self):
        task = {
            "id": 402,
            "description": "hopeless task",
            "agent": "developer",
            "retry_count": MAX_RETRIES,
            "status": "failed",
        }
        mock_fail = {"task_id": 403, "success": False, "output": "", "error": "Persistent failure"}

        with patch('tools.orchestrator_tool.registry_update'):
            with patch('tools.orchestrator_tool.delegate_task', return_value=mock_fail):
                with patch('tools.orchestrator_tool.list_roles', return_value=["developer", "qa"]):
                    with patch('tools.orchestrator_tool.store_insight') as mock_store:
                        with patch('tools.orchestrator_tool.append_event'):
                            result = handle_failure(task, "persistent failure")

        assert result["action"] == "escalate"
        assert result["success"] is False
        mock_store.assert_called_once()
        assert "escalation" in mock_store.call_args[0][0].lower()

    def test_orchestration_handles_mixed_results(self):
        mock_decomposition = [
            {"task": "easy task", "role": "developer", "dependencies": []},
            {"task": "hard task", "role": "developer", "dependencies": []},
            {"task": "verification", "role": "qa", "dependencies": [0, 1]},
        ]

        mock_task_obj = {
            "id": 405,
            "description": "hard task",
            "agent": "developer",
            "retry_count": 0,
            "status": "failed",
        }

        delegations = [
            {"task_id": 404, "success": True, "output": "Easy done.", "error": None},
            {"task_id": 405, "success": False, "output": "", "error": "Hard fail"},
            {"task_id": 406, "success": True, "output": "QA passed.", "error": None},
        ]
        delegation_iter = iter(delegations)

        recovery = {
            "action": "retry",
            "task_id": 405,
            "success": True,
            "output": "Hard task fixed.",
            "error": None,
        }

        def side_effect_delegate(*args, **kwargs):
            return next(delegation_iter)

        with patch('tools.orchestrator_tool.decompose_goal', return_value=mock_decomposition):
            with patch('tools.orchestrator_tool.delegate_task', side_effect=side_effect_delegate):
                with patch('tools.orchestrator_tool.get_task_by_id', return_value=mock_task_obj):
                    with patch('tools.orchestrator_tool.handle_failure', return_value=recovery):
                        with patch('tools.orchestrator_tool.store_insight'):
                            with patch('tools.orchestrator_tool.append_event'):
                                result = run_orchestration("Mixed scenario")

        assert result["summary"]["total"] == 3
        assert result["summary"]["succeeded"] == 3
        assert result["summary"]["failed"] == 0
        assert result["results"][1]["recovery_action"] == "retry"
