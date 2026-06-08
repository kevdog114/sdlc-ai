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
    _execute_story_tasks,
    _continue_to_architect,
    _continue_to_execution,
    PROJECTS_DIR,
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

    def test_submit_no_ambiguities(self):
        """Project without ambiguities should proceed to architect."""
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

        with patch('tools.project_tool._ba_analyze', return_value=mock_ba):
            with patch('tools.project_tool._architect_design', return_value=mock_arch):
                with patch('tools.project_tool._create_stories_and_tasks', return_value=[
                    {"title": "Feature", "description": "", "acceptance_criteria": [], "priority": "medium", "tasks": []},
                ]):
                    with patch('tools.project_tool._execute_story_tasks', return_value={
                        "story_id": "STORY-1", "task_count": 0, "succeeded": 0, "failed": 0, "all_succeeded": True, "task_results": [],
                    }):
                        with patch('tools.project_tool.append_event'):
                            with patch('tools.project_tool.save_json'):
                                with patch('tools.project_tool.load_json', return_value={"version": "1.0.0"}):
                                    with patch('tools.project_tool.load_project_state', return_value={}):
                                        with patch('tools.project_tool.save_project_state'):
                                            result = submit_project("Build a todo app")

        assert result["success"] is True
        assert result["status"] == "completed"

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
        mock_dir = MagicMock()
        mock_dir.glob.return_value = [
            Path("/fake/proj-abc.json"),
            Path("/fake/proj-def.json"),
        ]

        def fake_load(path, default=None):
            data = {
                "/fake/proj-abc.json": {
                    "id": "proj-abc",
                    "description": "First project",
                    "phase": "ba_analysis",
                    "status": "active",
                    "created_at": "2026-01-01",
                },
                "/fake/proj-def.json": {
                    "id": "proj-def",
                    "description": "Second project",
                    "phase": "complete",
                    "status": "completed",
                    "created_at": "2026-01-02",
                },
            }
            return data.get(str(path), default)

        with patch('tools.project_tool.PROJECTS_DIR', mock_dir):
            with patch('tools.project_tool.load_json', side_effect=fake_load):
                projects = list_projects()

        assert len(projects) == 2
        assert projects[0]["id"] == "proj-abc"
        assert projects[1]["id"] == "proj-def"

    def test_empty_when_no_projects(self):
        mock_dir = MagicMock()
        mock_dir.glob.return_value = []
        with patch('tools.project_tool.PROJECTS_DIR', mock_dir):
            projects = list_projects()
        assert projects == []


class TestExecuteStoryTasks:
    def test_executes_tasks(self):
        story = {
            "title": "Auth Feature",
            "description": "User authentication",
            "acceptance_criteria": ["Login", "Logout"],
            "priority": "high",
            "tasks": [
                {"description": "Create login page", "role": "developer", "dependencies": []},
            ],
        }

        with patch('tools.story_tool.create_story') as mock_create:
            mock_create.return_value = {"id": "STORY-1"}
            with patch('tools.orchestrator_tool.delegate_task') as mock_delegate:
                mock_delegate.return_value = {"task_id": 100, "success": True, "output": "Done.", "error": None}
                with patch('tools.story_tool.add_task_to_story'):
                    with patch('tools.project_tool.load_project_state', return_value={}):
                        with patch('tools.project_tool.save_project_state'):
                            result = _execute_story_tasks(story, "proj-test", "")

        assert result["story_id"] == "STORY-1"
        assert result["task_count"] == 1
        assert result["succeeded"] == 1
        assert result["all_succeeded"] is True

    def test_handles_failures(self):
        story = {
            "title": "Failing Feature",
            "tasks": [
                {"description": "Task 1", "role": "developer", "dependencies": []},
                {"description": "Task 2", "role": "developer", "dependencies": []},
            ],
        }

        delegations = [
            {"task_id": 101, "success": True, "output": "OK", "error": None},
            {"task_id": 102, "success": False, "output": "", "error": "Failed"},
        ]
        delegation_iter = iter(delegations)

        def side_effect(*args, **kwargs):
            return next(delegation_iter)

        with patch('tools.story_tool.create_story') as mock_create:
            mock_create.return_value = {"id": "STORY-2"}
            with patch('tools.orchestrator_tool.delegate_task', side_effect=side_effect):
                with patch('tools.story_tool.add_task_to_story'):
                    with patch('tools.project_tool.load_project_state', return_value={}):
                        with patch('tools.project_tool.save_project_state'):
                            result = _execute_story_tasks(story, "proj-test", "")

        assert result["task_count"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert result["all_succeeded"] is False
