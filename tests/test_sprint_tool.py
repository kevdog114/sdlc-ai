"""Tests for the sprint lifecycle: plan → start/execute → review → accept →
complete (velocity) → retrospective, plus delta change requests."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap
from tools.story_tool import create_story, get_story, list_backlog, update_story_fields
from tools.sprint_tool import (
    plan_sprint,
    cancel_sprint,
    start_sprint,
    review_sprint,
    accept_story,
    complete_sprint,
    get_sprint,
    get_velocity,
    run_retrospective,
)


def _story(title, project="proj-1", points=3, priority="medium", deps=None, tasks=None):
    return create_story(
        title=title,
        priority=priority,
        dependencies=deps or [],
        project_id=project,
        story_points=points,
        planned_tasks=tasks if tasks is not None else [
            {"description": f"Do {title}", "role": "researcher", "dependencies": []}
        ],
    )


GOAL_LLM = {"success": True, "content": "Deliver the increment"}


class TestPlanSprint:
    def test_plan_selects_by_priority_within_capacity(self):
        _story("Low prio", points=3, priority="low")
        _story("High prio", points=3, priority="high")
        _story("Medium prio", points=3, priority="medium")

        with patch("tools.sprint_tool.query_llm", return_value=GOAL_LLM):
            result = plan_sprint("proj-1", capacity_points=6)

        assert result["success"] is True
        sprint = result["sprint"]
        titles = [get_story(sid)["title"] for sid in sprint["story_ids"]]
        assert titles == ["High prio", "Medium prio"]
        assert sprint["committed_points"] == 6
        assert sprint["status"] == "planning"

        # Selected stories left the open backlog; the low-prio one remains.
        remaining = [s["title"] for s in list_backlog("proj-1")]
        assert remaining == ["Low prio"]

    def test_plan_respects_dependencies(self):
        a = _story("Foundation", points=3, priority="low")
        _story("Depends on foundation", points=3, priority="high", deps=[a["id"]])

        with patch("tools.sprint_tool.query_llm", return_value=GOAL_LLM):
            result = plan_sprint("proj-1")

        order = [get_story(sid)["title"] for sid in result["sprint"]["story_ids"]]
        # The dependent story is selectable only after its dependency.
        assert order.index("Foundation") < order.index("Depends on foundation")

    def test_plan_scoped_to_project(self):
        _story("Mine", project="proj-1")
        _story("Other project", project="proj-2")

        with patch("tools.sprint_tool.query_llm", return_value=GOAL_LLM):
            result = plan_sprint("proj-1")

        titles = [get_story(sid)["title"] for sid in result["sprint"]["story_ids"]]
        assert titles == ["Mine"]

    def test_plan_empty_backlog(self):
        result = plan_sprint("proj-empty")
        assert result["success"] is False

    def test_cancel_returns_stories_to_backlog(self):
        _story("Parked")
        with patch("tools.sprint_tool.query_llm", return_value=GOAL_LLM):
            planned = plan_sprint("proj-1")
        sprint_id = planned["sprint"]["id"]
        assert list_backlog("proj-1") == []

        assert cancel_sprint(sprint_id)["success"] is True
        assert get_sprint(sprint_id)["status"] == "cancelled"
        assert [s["title"] for s in list_backlog("proj-1")] == ["Parked"]


class TestSprintExecution:
    def _plan(self):
        with patch("tools.sprint_tool.query_llm", return_value=GOAL_LLM):
            return plan_sprint("proj-1")["sprint"]

    def test_start_executes_planned_tasks_via_gates(self):
        _story("Build feature", tasks=[
            {"description": "Implement", "role": "researcher", "dependencies": []},
            {"description": "Verify", "role": "qa", "dependencies": [0]},
        ])
        sprint = self._plan()

        def fake_delegate(description, role, **kwargs):
            task_id = kwargs.get("existing_task_id")
            bootstrap.update_task_status(task_id, "done")
            return {"task_id": task_id, "success": True, "output": "ok", "error": None}

        with patch("tools.orchestrator_tool.delegate_task", side_effect=fake_delegate):
            result = start_sprint(sprint["id"])

        assert result["success"] is True
        refreshed = get_sprint(sprint["id"])
        assert refreshed["status"] == "review"
        assert refreshed["story_results"][0]["all_succeeded"] is True
        assert refreshed["story_results"][0]["task_count"] == 2

        # Real registry tasks were materialized from planned_tasks and linked.
        story = get_story(refreshed["story_ids"][0])
        assert len(story["task_ids"]) == 2
        for tid in story["task_ids"]:
            assert bootstrap.get_task(tid)["story_id"] == story["id"]

    def test_intra_story_dependency_skips_after_failure(self):
        _story("Fragile", tasks=[
            {"description": "Base", "role": "researcher", "dependencies": []},
            {"description": "On top", "role": "researcher", "dependencies": [0]},
        ])
        sprint = self._plan()

        def failing_delegate(description, role, **kwargs):
            return {"task_id": kwargs.get("existing_task_id"), "success": False,
                    "output": "", "error": "nope"}

        with patch("tools.orchestrator_tool.delegate_task", side_effect=failing_delegate):
            result = start_sprint(sprint["id"])

        story_result = result["sprint"]["story_results"][0]
        assert story_result["succeeded"] == 0
        # Second task skipped, not executed against a broken base.
        assert story_result["task_results"][1]["skipped"] is True

    def test_story_with_unmet_dependency_is_skipped(self):
        a = _story("Foundation")
        _story("Dependent", deps=[a["id"]])
        sprint = self._plan()

        def failing_delegate(description, role, **kwargs):
            return {"task_id": kwargs.get("existing_task_id"), "success": False,
                    "output": "", "error": "nope"}

        with patch("tools.orchestrator_tool.delegate_task", side_effect=failing_delegate):
            result = start_sprint(sprint["id"])

        by_title = {r["title"]: r for r in result["sprint"]["story_results"]}
        assert by_title["Dependent"].get("skipped") is True
        assert by_title["Dependent"]["reason"] == "unmet story dependency"

    def test_cannot_start_twice(self):
        _story("Once")
        sprint = self._plan()
        with patch("tools.orchestrator_tool.delegate_task",
                   return_value={"task_id": 1, "success": True, "output": "", "error": None}):
            start_sprint(sprint["id"])
        again = start_sprint(sprint["id"])
        assert again["success"] is False


class TestReviewAcceptComplete:
    def _reviewed_sprint(self, titles_points):
        for title, points in titles_points:
            _story(title, points=points)
        with patch("tools.sprint_tool.query_llm", return_value=GOAL_LLM):
            sprint = plan_sprint("proj-1")["sprint"]

        def ok_delegate(description, role, **kwargs):
            tid = kwargs.get("existing_task_id")
            bootstrap.update_task_status(tid, "done")
            return {"task_id": tid, "success": True, "output": "ok", "error": None}

        with patch("tools.orchestrator_tool.delegate_task", side_effect=ok_delegate):
            start_sprint(sprint["id"])
        return get_sprint(sprint["id"])

    def test_review_lists_stories_for_acceptance(self):
        sprint = self._reviewed_sprint([("A", 3), ("B", 5)])
        review = review_sprint(sprint["id"])
        assert review["success"] is True
        assert len(review["stories"]) == 2
        assert all(s["po_acceptance"] is None for s in review["stories"])
        assert all(s["derived_status"] == "done" for s in review["stories"])

    def test_velocity_counts_only_accepted_points(self):
        sprint = self._reviewed_sprint([("A", 3), ("B", 5)])
        sid_a, sid_b = sprint["story_ids"]

        accept_story(sid_a, True)
        accept_story(sid_b, False, notes="Not what I wanted")

        with patch("tools.sprint_tool.query_llm",
                   return_value={"success": True, "content": "[]"}):
            result = complete_sprint(sprint["id"])

        assert result["success"] is True
        assert result["velocity_points"] == 3  # only the accepted story
        assert get_sprint(sprint["id"])["status"] == "complete"

        # The rejected story returned to the backlog carrying the PO's notes.
        rejected = get_story(sid_b)
        assert rejected["sprint_id"] is None
        assert rejected["po_acceptance"] == "rejected"
        assert rejected["po_notes"] == "Not what I wanted"
        assert sid_b in [s["id"] for s in list_backlog("proj-1")]

    def test_rolling_velocity(self):
        for i in range(2):
            sprint = self._reviewed_sprint([(f"S{i}", 5)])
            accept_story(sprint["story_ids"][0], True)
            with patch("tools.sprint_tool.query_llm",
                       return_value={"success": True, "content": "[]"}):
                complete_sprint(sprint["id"])

        velocity = get_velocity("proj-1")
        assert velocity["sprints_counted"] == 2
        assert velocity["velocity"] == 5.0


class TestRetrospective:
    def test_retro_mines_events_and_stores_proposals(self):
        _story("Retro story")
        with patch("tools.sprint_tool.query_llm", return_value=GOAL_LLM):
            sprint = plan_sprint("proj-1")["sprint"]

        def ok_delegate(description, role, **kwargs):
            tid = kwargs.get("existing_task_id")
            bootstrap.update_task_status(tid, "done")
            return {"task_id": tid, "success": True, "output": "ok", "error": None}

        with patch("tools.orchestrator_tool.delegate_task", side_effect=ok_delegate):
            start_sprint(sprint["id"])

        # Seed sprint-window events the miner should count.
        bootstrap.append_event("tool:stage_gate", {"action": "qa_fail", "task_id": 1})
        bootstrap.append_event("tool:orchestrator", {"action": "delegate_task_retry", "task_id": 1})
        bootstrap.append_event("tool:orchestrator", {"action": "blocked_needs_human", "task_id": 2})

        proposals = [
            {"area": "qa gate", "proposal": "Tighten acceptance criteria in refinement",
             "expected_effect": "fewer QA rejections"},
        ]
        with patch("tools.sprint_tool.query_llm",
                   return_value={"success": True, "content": json.dumps(proposals)}), \
             patch("tools.knowledge_tool.query_llm",
                   return_value={"success": False, "content": ""}):
            retro = run_retrospective(sprint["id"])

        assert retro["success"] is True
        assert retro["stats"]["qa_gate_failures"] == 1
        assert retro["stats"]["task_retries"] == 1
        assert retro["stats"]["tasks_blocked_for_human"] == 1
        assert len(retro["proposals"]) == 1
        assert retro["proposals"][0]["status"] == "proposed"
        assert get_sprint(sprint["id"])["retrospective"]["proposals"]

    def test_retro_caps_at_three_proposals(self):
        _story("Cap story")
        with patch("tools.sprint_tool.query_llm", return_value=GOAL_LLM):
            sprint = plan_sprint("proj-1")["sprint"]
        five = [{"area": f"a{i}", "proposal": f"p{i}", "expected_effect": "e"} for i in range(5)]
        with patch("tools.sprint_tool.query_llm",
                   return_value={"success": True, "content": json.dumps(five)}):
            retro = run_retrospective(sprint["id"])
        assert len(retro["proposals"]) == 3


class TestChangeRequests:
    def _defined_project(self):
        from tools.project_tool import create_project_record, _save_project
        project = create_project_record("A todo app", "Todo")
        project["refined_requirements"] = "Todo app with add/list."
        project["architecture"] = "Flask + SQLite."
        _save_project(project)
        # Existing story in the ledger
        story = _story("Add todos", project=project["id"])
        project["story_ids"] = [story["id"]]
        _save_project(project)
        return project

    def test_change_request_appends_delta_stories(self):
        from tools.project_tool import submit_change_request, _load_project
        project = self._defined_project()

        delta = {
            "impact_summary": "Adds a delete capability.",
            "stories": [
                {"title": "Delete todos", "description": "", "acceptance_criteria": ["can delete"],
                 "priority": "high", "story_points": 2, "supersedes": None,
                 "tasks": [{"description": "Add delete endpoint", "role": "developer", "dependencies": []}]},
            ],
            "ambiguities": [],
        }
        llm = MagicMock(return_value={"success": True, "content": json.dumps(delta)})
        with patch("tools.project_tool.query_llm", llm):
            result = submit_change_request(project["id"], "Let me delete todos")

        assert result["success"] is True
        assert result["status"] == "accepted_into_backlog"
        assert len(result["story_ids"]) == 1

        # The BA saw the existing ledger — the prompt carries the prior story.
        prompt = llm.call_args[0][0]
        assert "Add todos" in prompt
        assert "Let me delete todos" in prompt

        # Appended, not overwritten.
        refreshed = _load_project(project["id"])
        assert len(refreshed["story_ids"]) == 2
        assert refreshed["change_requests"][0]["status"] == "accepted_into_backlog"

    def test_change_request_with_ambiguities_pauses(self):
        from tools.project_tool import submit_change_request
        project = self._defined_project()

        delta = {
            "impact_summary": "",
            "stories": [],
            "ambiguities": [{"question": "Soft or hard delete?", "context": "", "reason": "data model"}],
        }
        with patch("tools.project_tool.query_llm",
                   return_value={"success": True, "content": json.dumps(delta)}):
            result = submit_change_request(project["id"], "Let me delete todos")

        assert result["success"] is True
        assert result["status"] == "awaiting_clarification"
        from tools.clarification_tool import list_requests
        pending = list_requests(project_id=project["id"], status="pending")
        assert len(pending) == 1
        assert "delete" in pending[0]["question"].lower()

    def test_change_request_requires_defined_project(self):
        from tools.project_tool import submit_change_request, create_project_record
        project = create_project_record("Bare", "Bare")
        result = submit_change_request(project["id"], "Change something")
        assert result["success"] is False