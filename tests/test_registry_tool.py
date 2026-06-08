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
    def test_update_existing_task(self):
        registry = {
            "tasks": [{"id": 1, "status": "pending", "description": "task 1"}],
            "next_id": 2,
        }
        with patch('tools.registry_tool.load_json') as mock_load:
            mock_load.return_value = registry
            with patch('tools.registry_tool.save_json') as mock_save:
                with patch('tools.registry_tool.append_event'):
                    result = update_task_status(1, "done", "completed")
            assert result is True
            assert registry["tasks"][0]["status"] == "done"
            assert registry["tasks"][0]["completion_notes"] == "completed"
            mock_save.assert_called_once()

    def test_update_nonexistent_task(self):
        with patch('tools.registry_tool.load_json') as mock_load:
            mock_load.return_value = {"tasks": [{"id": 1, "status": "pending"}]}
            with patch('tools.registry_tool.append_event'):
                result = update_task_status(999, "done")
            assert result is False

    def test_update_task_no_notes(self):
        registry = {
            "tasks": [{"id": 1, "status": "pending", "description": "task 1"}],
        }
        with patch('tools.registry_tool.load_json') as mock_load:
            mock_load.return_value = registry
            with patch('tools.registry_tool.save_json'):
                with patch('tools.registry_tool.append_event'):
                    result = update_task_status(1, "in_progress")
            assert result is True
            assert registry["tasks"][0]["status"] == "in_progress"

    def test_update_task_exception(self):
        with patch('tools.registry_tool.load_json', side_effect=Exception("json error")):
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
            mock_add.assert_called_once_with(description="new task", agent="test-agent", story_id=None, interface_spec_id=None)

    def test_create_task_exception(self):
        with patch('tools.registry_tool.add_task', side_effect=Exception("add error")):
            with patch('tools.registry_tool.append_event'):
                result = create_new_task("failing task")
            assert result["success"] is False
            assert "error" in result
