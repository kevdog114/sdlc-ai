import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.registry_tool import (
    get_pending_tasks,
    get_task_by_id,
    update_task_status,
    create_new_task,
)


class TestGetPendingTasks:
    def test_get_pending_tasks_success(self):
        with patch('tools.registry_tool.load_json') as mock_load:
            mock_load.return_value = {
                "tasks": [
                    {"id": 1, "status": "pending", "description": "task a"},
                    {"id": 2, "status": "done", "description": "task b"},
                    {"id": 3, "status": "pending", "description": "task c"},
                ]
            }
            with patch('tools.registry_tool.append_event'):
                result = get_pending_tasks()
            assert len(result) == 2
            assert all(t["status"] == "pending" for t in result)

    def test_get_pending_tasks_empty(self):
        with patch('tools.registry_tool.load_json') as mock_load:
            mock_load.return_value = {"tasks": []}
            with patch('tools.registry_tool.append_event'):
                result = get_pending_tasks()
            assert result == []

    def test_get_pending_tasks_exception(self):
        with patch('tools.registry_tool.load_json', side_effect=Exception("json error")):
            with patch('tools.registry_tool.append_event'):
                result = get_pending_tasks()
            assert result == []


class TestGetTaskById:
    def test_get_existing_task(self):
        with patch('tools.registry_tool.load_json') as mock_load:
            mock_load.return_value = {
                "tasks": [
                    {"id": 1, "status": "pending", "description": "task 1"},
                    {"id": 2, "status": "done", "description": "task 2"},
                ]
            }
            with patch('tools.registry_tool.append_event'):
                result = get_task_by_id(1)
            assert result is not None
            assert result["id"] == 1

    def test_get_nonexistent_task(self):
        with patch('tools.registry_tool.load_json') as mock_load:
            mock_load.return_value = {"tasks": [{"id": 1, "status": "pending"}]}
            with patch('tools.registry_tool.append_event'):
                result = get_task_by_id(999)
            assert result is None

    def test_get_task_exception(self):
        with patch('tools.registry_tool.load_json', side_effect=Exception("json error")):
            with patch('tools.registry_tool.append_event'):
                result = get_task_by_id(1)
            assert result is None


class TestUpdateTaskStatus:
    # update_task_status now delegates to bootstrap's single implementation
    # (kanban sync, per-task file, event log), so these tests run against the
    # real (hermetic) state instead of patching internals.

    def test_update_existing_task(self):
        import bootstrap
        task = bootstrap.add_task("task 1", agent="developer")
        result = update_task_status(task["id"], "done", "completed")
        assert result is True
        refreshed = bootstrap.get_task(task["id"])
        assert refreshed["status"] == "done"
        assert refreshed["completion_notes"] == "completed"

    def test_update_nonexistent_task(self):
        result = update_task_status(999, "done")
        assert result is False

    def test_update_task_no_notes(self):
        import bootstrap
        task = bootstrap.add_task("task 1", agent="developer")
        result = update_task_status(task["id"], "in_progress")
        assert result is True
        refreshed = bootstrap.get_task(task["id"])
        assert refreshed["status"] == "in_progress"
        assert refreshed["completion_notes"] is None

    def test_update_task_exception(self):
        with patch('bootstrap.update_task_status', side_effect=Exception("boom")):
            with patch('tools.registry_tool.append_event'):
                result = update_task_status(1, "done")
            assert result is False


class TestCreateNewTask:
    def test_create_task_success(self):
        mock_task = {"id": 10, "description": "new task", "status": "pending"}
        with patch('tools.registry_tool.add_task') as mock_add:
            mock_add.return_value = mock_task
            with patch('tools.registry_tool.append_event'):
                result = create_new_task("new task", agent="test-agent")
            assert result == mock_task
            mock_add.assert_called_once_with(description="new task", agent="test-agent", story_id=None, interface_spec_id=None, project_id=None)

    def test_create_task_exception(self):
        with patch('tools.registry_tool.add_task', side_effect=Exception("add error")):
            with patch('tools.registry_tool.append_event'):
                result = create_new_task("failing task")
            assert result["success"] is False
            assert "error" in result
