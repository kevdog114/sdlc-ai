"""Tests for the Project Tool (SDLC Pipeline Entry Point)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.project_tool import (
    submit_project,
    resume_project,
    get_project_status,
    list_projects,
    _ba_analyze,
    _architect_design,
    _create_stories_and_tasks,
    _persist_backlog_stories,
    _continue_to_architect,
    _define_backlog,
)


class TestBAnalyze:
    def test_returns_requirements_and_ambiguities(self):
        mock_llm = {
            "success": True,
            "content": json.dumps({
                "refined_requirements": "Build a web app with user auth.",
                "ambiguities": [
                    {"question": "What database?", "context": "Needs data storage.", "reason": "Required for design."}
                ],
            }),
        }
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                result = _ba_analyze("Build a web app")

        assert result["success"] is True
        assert "web app" in result["refined_requirements"]
        assert len(result["ambiguities"]) == 1
        assert result["ambiguities"][0]["question"] == "What database?"

    def test_no_ambiguities(self):
        mock_llm = {
            "success": True,
            "content": json.dumps({
                "refined_requirements": "Build a todo app.",
                "ambiguities": [],
            }),
        }
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                result = _ba_analyze("Build a todo app")

        assert result["success"] is True
        assert len(result["ambiguities"]) == 0

    def test_llm_failure(self):
        mock_llm = {"success": False, "error": "LLM unavailable"}
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                result = _ba_analyze("Test")

        assert result["success"] is False
        assert "error" in result

    def test_invalid_json_fallback(self):
        mock_llm = {"success": True, "content": "not json at all"}
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                result = _ba_analyze("Test")

        assert result["success"] is True
        assert result["refined_requirements"] == "not json at all"
        assert result["ambiguities"] == []


class TestArchitectDesign:
    def test_returns_architecture_and_spec(self):
        mock_llm = {
            "success": True,
            "content": json.dumps({
                "architecture": "Microservices with REST APIs.",
                "interface_spec": "openapi: 3.0.0\ninfo:\n  title: API",
            }),
        }
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                result = _architect_design("Build a scalable system")

        assert result["success"] is True
        assert "Microservices" in result["architecture"]
        assert "openapi" in result["interface_spec"]

    def test_llm_failure(self):
        mock_llm = {"success": False, "error": "LLM down"}
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                result = _architect_design("Test")

        assert result["success"] is False

    def test_invalid_json_fallback(self):
        mock_llm = {"success": True, "content": "bad json"}
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                result = _architect_design("Test")

        assert result["success"] is False


class TestCreateStoriesAndTasks:
    def test_creates_stories(self):
        mock_llm = {
            "success": True,
            "content": json.dumps([
                {
                    "title": "User Authentication",
                    "description": "Users can sign up and log in.",
                    "acceptance_criteria": ["Can register", "Can log in"],
                    "priority": "high",
                    "tasks": [
                        {"description": "Create auth module", "role": "developer", "dependencies": []},
                    ],
                },
            ]),
        }
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                stories = _create_stories_and_tasks("Arch", "Requirements", "")

        assert len(stories) == 1
        assert stories[0]["title"] == "User Authentication"
        assert len(stories[0]["tasks"]) == 1

    def test_llm_failure_returns_empty(self):
        mock_llm = {"success": False, "error": "LLM down"}
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                stories = _create_stories_and_tasks("Arch", "Req", "")

        assert stories == []

    def test_invalid_json_returns_empty(self):
        mock_llm = {"success": True, "content": "not json"}
        with patch('tools.project_tool.query_llm', return_value=mock_llm):
            with patch('tools.project_tool.append_event'):
                stories = _create_stories_and_tasks("Arch", "Req", "")

        assert stories == []


class TestSubmitProject:
    def test_submit_with_ambiguities(self):
        """Project with ambiguities should pause for clarification."""
        mock_ba = {
            "success": True,
            "refined_requirements": "Build a web app.",
            "ambiguities": [
                {"question": "What database?", "context": "Storage needed.", "reason": "Design decision."},
            ],
        }

        with patch('tools.project_tool._ba_analyze', return_value=mock_ba):
            with patch('tools.project_tool.append_event'):
                with patch('tools.clarification_tool.create_request') as mock_create:
                    mock_create.return_value = {"id": "CLR-0001"}
                    result = submit_project("Build a web app")

        assert result["success"] is True
        assert result["status"] == "awaiting_clarification"
        assert result["ambiguity_count"] == 1
        assert "clarification_ids" in result

    def test_submit_defines_backlog_without_executing(self):
        """Submitting a project ends with a persisted backlog + proposed
        sprint — no tasks are created and nothing executes (define/execute
        separation)."""
        import bootstrap
        mock_ba = {
            "success": True,
            "refined_requirements": "Build a todo app.",
            "ambiguities": [],
        }
        mock_arch = {
            "success": True,
            "architecture": "Simple CRUD app.",
            "interface_spec": "openapi: 3.0.0",
        }
        story_specs = [
            {"title": "Add todos", "description": "", "acceptance_criteria": ["can add"],
             "priority": "high", "story_points": 3,
             "tasks": [{"description": "Implement add endpoint", "role": "developer", "dependencies": []}]},
            {"title": "List todos", "description": "", "acceptance_criteria": [],
             "priority": "medium", "story_points": 2,
             "tasks": [{"description": "Implement list endpoint", "role": "developer", "dependencies": []}]},
        ]

        with patch('tools.project_tool._ba_analyze', return_value=mock_ba), \
             patch('tools.project_tool._architect_design', return_value=mock_arch), \
             patch('tools.project_tool._create_stories_and_tasks', return_value=story_specs), \
             patch('tools.sprint_tool.query_llm', return_value={"success": True, "content": "Ship todo basics"}):
            result = submit_project("Build a todo app", name="Todo")

        assert result["success"] is True
        assert result["phase"] == "backlog_ready"
        assert len(result["story_ids"]) == 2
        assert result["proposed_sprint_id"] is not None
        assert result["auto_executed"] is False

        # Stories persisted to the real (hermetic) registry, project-scoped,
        # with deferred task specs — and NO registry tasks were created.
        from tools.story_tool import get_story
        story = get_story(result["story_ids"][0])
        assert story["project_id"] == result["project_id"]
        assert story["story_points"] == 3
        assert story["planned_tasks"][0]["description"] == "Implement add endpoint"
        assert story["task_ids"] == []
        assert bootstrap.list_tasks() == []

        # The proposed sprint sits in planning, holding both stories.
        from tools.sprint_tool import get_sprint
        sprint = get_sprint(result["proposed_sprint_id"])
        assert sprint["status"] == "planning"
        assert set(sprint["story_ids"]) == set(result["story_ids"])

    def test_submit_ba_failure(self):
        mock_ba = {"success": False, "error": "BA analysis failed"}
        with patch('tools.project_tool._ba_analyze', return_value=mock_ba):
            with patch('tools.project_tool.append_event'):
                with patch('tools.project_tool.save_json'):
                    result = submit_project("Test")

        assert result["success"] is False
        assert "BA analysis failed" in result.get("error", "")


class TestResumeProject:
    def test_resume_when_not_awaiting(self):
        """Resuming a project that isn't paused should return error."""
        with patch('tools.project_tool._load_project', return_value={
            "id": "proj-test",
            "status": "completed",
            "phase": "complete",
        }):
            result = resume_project("proj-test")

        assert result["success"] is False

    def test_resume_when_still_pending(self):
        """If clarifications still pending, should indicate still waiting."""
        with patch('tools.project_tool._load_project', return_value={
            "id": "proj-test",
            "status": "awaiting_clarification",
            "phase": "ba_analysis",
            "refined_requirements": "",
        }):
            with patch('tools.clarification_tool.has_pending', return_value=True):
                result = resume_project("proj-test")

        assert result["success"] is True
        assert result["status"] == "awaiting_clarification"

    def test_resume_when_all_answered(self):
        """When all clarifications answered, should proceed."""
        with patch('tools.project_tool._load_project', return_value={
            "id": "proj-test",
            "status": "awaiting_clarification",
            "phase": "ba_analysis",
            "refined_requirements": "Build a web app.",
        }):
            with patch('tools.clarification_tool.has_pending', return_value=False):
                with patch('tools.project_tool._continue_to_architect') as mock_continue:
                    mock_continue.return_value = {"success": True, "status": "completed"}
                    with patch('tools.project_tool.append_event'):
                        result = resume_project("proj-test")

        assert result["success"] is True
        mock_continue.assert_called_once()


class TestGetProjectStatus:
    def test_returns_project_status(self):
        project = {
            "id": "proj-test",
            "description": "Build a web app",
            "phase": "ba_analysis",
            "status": "awaiting_clarification",
            "results": [],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        with patch('tools.project_tool._load_project', return_value=project):
            with patch('tools.clarification_tool.count_by_status', return_value={"pending": 2, "answered": 0, "cancelled": 0}):
                with patch('tools.clarification_tool.list_requests', return_value=[
                    {"id": "CLR-0001", "question": "Q?", "created_at": "..."},
                ]):
                    status = get_project_status("proj-test")

        assert status is not None
        assert status["id"] == "proj-test"
        assert status["status"] == "awaiting_clarification"
        assert status["clarifications"]["pending"] == 2

    def test_returns_none_for_missing(self):
        with patch('tools.project_tool._load_project', return_value=None):
            status = get_project_status("nonexistent")
        assert status is None


class TestListProjects:
    def test_lists_projects(self):
        from tools.project_tool import create_project_record

        create_project_record("First project", "First")
        create_project_record("Second project", "Second")

        projects = list_projects()

        assert len(projects) == 2
        assert all(p["id"].startswith("proj-") for p in projects)
        names = {p["name"] for p in projects}
        assert names == {"First", "Second"}

    def test_empty_when_no_projects(self):
        assert list_projects() == []


class TestPersistBacklogStories:
    def test_persists_specs_as_planned_tasks(self):
        story_ids = _persist_backlog_stories(
            [
                {"title": "Feature A", "priority": "high", "story_points": 5,
                 "tasks": [
                     {"description": "Build it", "role": "developer", "dependencies": []},
                     {"description": "Test it", "role": "qa", "dependencies": [0]},
                 ]},
            ],
            "proj-x",
        )
        assert len(story_ids) == 1
        from tools.story_tool import get_story
        story = get_story(story_ids[0])
        assert story["project_id"] == "proj-x"
        assert story["story_points"] == 5
        assert [t["role"] for t in story["planned_tasks"]] == ["developer", "qa"]
        assert story["planned_tasks"][1]["dependencies"] == [0]

    def test_invalid_points_snapped_to_scale(self):
        story_ids = _persist_backlog_stories(
            [{"title": "Big", "story_points": 7, "tasks": [{"description": "x", "role": "developer"}]}],
            "proj-x",
        )
        from tools.story_tool import get_story
        assert get_story(story_ids[0])["story_points"] == 8
