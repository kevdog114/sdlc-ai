"""Tests for tools/story_tool.py — story CRUD, auto-derivation, progress."""

import json
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import bootstrap
from tools import story_tool


class _TestSetup:
    """Shared setup/teardown for story tests."""

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
        story_tool.STORY_REGISTRY_PATH = self.state_dir / "story_registry.json"
        story_tool.BASE_DIR = Path(self.tmpdir)

        bootstrap.init_task_registry()
        bootstrap.init_project_state()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ── CRUD ────────────────────────────────────────────────────────


class TestCreateStory(_TestSetup):
    def test_create_returns_valid_story(self):
        story = story_tool.create_story("Test Story", "As a user...", ["Criterion 1"], "high")
        assert story["id"] == "STORY-1"
        assert story["title"] == "Test Story"
        assert story["priority"] == "high"
        assert story["acceptance_criteria"] == ["Criterion 1"]
        assert story["task_ids"] == []
        assert story["completion_notes"] is None

    def test_create_increments_id(self):
        s1 = story_tool.create_story("First")
        s2 = story_tool.create_story("Second")
        assert s1["id"] == "STORY-1"
        assert s2["id"] == "STORY-2"

    def test_create_default_priority(self):
        story = story_tool.create_story("Default priority")
        assert story["priority"] == "medium"

    def test_create_invalid_priority_defaults_to_medium(self):
        story = story_tool.create_story("Invalid", priority="urgent")
        assert story["priority"] == "medium"


class TestGetStory(_TestSetup):
    def test_get_by_string_id(self):
        story = story_tool.create_story("Findable")
        found = story_tool.get_story("STORY-1")
        assert found is not None
        assert found["title"] == "Findable"

    def test_get_by_int_id(self):
        story = story_tool.create_story("Findable by int")
        found = story_tool.get_story(1)
        assert found is not None
        assert found["title"] == "Findable by int"

    def test_get_nonexistent(self):
        assert story_tool.get_story("STORY-999") is None


class TestListStories(_TestSetup):
    def test_list_all(self):
        story_tool.create_story("A")
        story_tool.create_story("B")
        stories = story_tool.list_stories()
        assert len(stories) == 2

    def test_list_filtered_by_status(self):
        story_tool.create_story("Backlog story")
        s2 = story_tool.create_story("Active story")
        task = bootstrap.add_task("Task for active")
        story_tool.add_task_to_story(s2["id"], task["id"])
        bootstrap.update_task_status(task["id"], "in_progress")
        stories = story_tool.list_stories(status="in_progress")
        assert len(stories) == 1
        assert stories[0]["id"] == s2["id"]

    def test_list_empty(self):
        assert story_tool.list_stories() == []


class TestUpdateStory(_TestSetup):
    def test_update_title(self):
        story_tool.create_story("Old Title")
        updated = story_tool.update_story("STORY-1", title="New Title")
        assert updated["title"] == "New Title"

    def test_update_priority(self):
        story_tool.create_story("Low priority", priority="low")
        updated = story_tool.update_story("STORY-1", priority="high")
        assert updated["priority"] == "high"

    def test_update_nonexistent(self):
        assert story_tool.update_story("STORY-999", title="X") is None

    def test_update_invalid_priority_ignored(self):
        story_tool.create_story("Test", priority="low")
        updated = story_tool.update_story("STORY-1", priority="invalid")
        assert updated["priority"] == "low"


class TestDeleteStory(_TestSetup):
    def test_delete_existing(self):
        story_tool.create_story("Delete me")
        assert story_tool.delete_story("STORY-1") is True
        assert story_tool.get_story("STORY-1") is None

    def test_delete_nonexistent(self):
        assert story_tool.delete_story("STORY-999") is False


# ── Task Linking ────────────────────────────────────────────────


class TestAddTaskToStory(_TestSetup):
    def test_add_task_links_both_ways(self):
        s = story_tool.create_story("Linked Story")
        t = bootstrap.add_task("Linked Task")
        assert story_tool.add_task_to_story(s["id"], t["id"]) is True

        story = story_tool.get_story(s["id"])
        assert t["id"] in story["task_ids"]

        task = bootstrap.get_task(t["id"])
        assert task["story_id"] == s["id"]

    def test_add_task_duplicate_noop(self):
        s = story_tool.create_story("Dup")
        t = bootstrap.add_task("Dup Task")
        story_tool.add_task_to_story(s["id"], t["id"])
        story_tool.add_task_to_story(s["id"], t["id"])
        story = story_tool.get_story(s["id"])
        assert story["task_ids"].count(t["id"]) == 1

    def test_add_task_nonexistent_story(self):
        t = bootstrap.add_task("Task")
        assert story_tool.add_task_to_story("STORY-999", t["id"]) is False


class TestRemoveTaskFromStory(_TestSetup):
    def test_remove_task_unlinks_both_ways(self):
        s = story_tool.create_story("Remove")
        t = bootstrap.add_task("Remove Task")
        story_tool.add_task_to_story(s["id"], t["id"])
        assert story_tool.remove_task_from_story(s["id"], t["id"]) is True

        story = story_tool.get_story(s["id"])
        assert t["id"] not in story["task_ids"]

        task = bootstrap.get_task(t["id"])
        assert task["story_id"] is None


class TestGetStoryTasks(_TestSetup):
    def test_returns_child_tasks(self):
        s = story_tool.create_story("Parent")
        t1 = bootstrap.add_task("Child 1")
        t2 = bootstrap.add_task("Child 2")
        story_tool.add_task_to_story(s["id"], t1["id"])
        story_tool.add_task_to_story(s["id"], t2["id"])

        tasks = story_tool.get_story_tasks(s["id"])
        assert len(tasks) == 2
        ids = {t["id"] for t in tasks}
        assert ids == {t1["id"], t2["id"]}

    def test_nonexistent_story(self):
        assert story_tool.get_story_tasks("STORY-999") == []


# ── Auto-Derived Status ────────────────────────────────────────


class TestDeriveStoryStatus(_TestSetup):
    def _create_story_with_tasks(self, statuses):
        s = story_tool.create_story("Status Test")
        for status in statuses:
            t = bootstrap.add_task(f"Task in {status}")
            story_tool.add_task_to_story(s["id"], t["id"])
            bootstrap.update_task_status(t["id"], status)
        return s["id"]

    def test_no_tasks_is_backlog(self):
        s = story_tool.create_story("Empty")
        assert story_tool.derive_story_status(s["id"]) == "backlog"

    def test_all_pending_is_backlog(self):
        sid = self._create_story_with_tasks(["pending", "pending"])
        assert story_tool.derive_story_status(sid) == "backlog"

    def test_any_in_progress(self):
        sid = self._create_story_with_tasks(["pending", "in_progress"])
        assert story_tool.derive_story_status(sid) == "in_progress"

    def test_any_testing(self):
        sid = self._create_story_with_tasks(["done", "testing"])
        assert story_tool.derive_story_status(sid) == "testing"

    def test_any_architect_review(self):
        sid = self._create_story_with_tasks(["done", "architect_review"])
        assert story_tool.derive_story_status(sid) == "architect_review"

    def test_all_done(self):
        sid = self._create_story_with_tasks(["done", "done"])
        assert story_tool.derive_story_status(sid) == "done"

    def test_all_completed(self):
        sid = self._create_story_with_tasks(["completed", "completed"])
        assert story_tool.derive_story_status(sid) == "done"

    def test_mixed_done_and_completed(self):
        sid = self._create_story_with_tasks(["done", "completed"])
        assert story_tool.derive_story_status(sid) == "done"

    def test_rejected_counts_as_backlog(self):
        sid = self._create_story_with_tasks(["rejected"])
        assert story_tool.derive_story_status(sid) == "backlog"

    def test_failed_counts_as_backlog(self):
        sid = self._create_story_with_tasks(["failed"])
        assert story_tool.derive_story_status(sid) == "backlog"

    def test_testing_passed_counts_as_testing(self):
        sid = self._create_story_with_tasks(["testing_passed"])
        assert story_tool.derive_story_status(sid) == "testing"

    def test_pending_verification_counts_as_testing(self):
        sid = self._create_story_with_tasks(["pending_verification"])
        assert story_tool.derive_story_status(sid) == "testing"

    def test_architect_review_takes_precedence_over_testing(self):
        sid = self._create_story_with_tasks(["testing", "architect_review"])
        assert story_tool.derive_story_status(sid) == "architect_review"

    def test_nonexistent_story_returns_backlog(self):
        assert story_tool.derive_story_status("STORY-999") == "backlog"


# ── Sync All Stories ───────────────────────────────────────────


class TestSyncAllStories(_TestSetup):
    def test_sync_creates_board(self):
        s1 = story_tool.create_story("Backlog")
        s2 = story_tool.create_story("Active")
        t = bootstrap.add_task("Active task")
        story_tool.add_task_to_story(s2["id"], t["id"])
        bootstrap.update_task_status(t["id"], "in_progress")

        board = story_tool.sync_all_stories()
        assert s1["id"] in board["backlog"]
        assert s2["id"] in board["in_progress"]

    def test_sync_persists_to_project_state(self):
        s = story_tool.create_story("Sync Test")
        story_tool.sync_all_stories()
        state = bootstrap.load_project_state()
        assert "stories_kanban" in state
        assert s["id"] in state["stories_kanban"]["backlog"]


# ── Progress ────────────────────────────────────────────────────


class TestGetStoryProgress(_TestSetup):
    def test_progress_empty_story(self):
        s = story_tool.create_story("Empty")
        prog = story_tool.get_story_progress(s["id"])
        assert prog["total_tasks"] == 0
        assert prog["percent"] == 0
        assert prog["status"] == "backlog"

    def test_progress_half_done(self):
        s = story_tool.create_story("Half")
        t1 = bootstrap.add_task("Done task")
        t2 = bootstrap.add_task("Pending task")
        story_tool.add_task_to_story(s["id"], t1["id"])
        story_tool.add_task_to_story(s["id"], t2["id"])
        bootstrap.update_task_status(t1["id"], "done")

        prog = story_tool.get_story_progress(s["id"])
        assert prog["total_tasks"] == 2
        assert prog["done_tasks"] == 1
        assert prog["percent"] == 50

    def test_progress_all_done(self):
        s = story_tool.create_story("All done")
        t1 = bootstrap.add_task("Task 1")
        t2 = bootstrap.add_task("Task 2")
        story_tool.add_task_to_story(s["id"], t1["id"])
        story_tool.add_task_to_story(s["id"], t2["id"])
        bootstrap.update_task_status(t1["id"], "done")
        bootstrap.update_task_status(t2["id"], "done")

        prog = story_tool.get_story_progress(s["id"])
        assert prog["percent"] == 100
        assert prog["status"] == "done"

    def test_nonexistent_story(self):
        prog = story_tool.get_story_progress("STORY-999")
        assert "error" in prog


# ── Complete Story ─────────────────────────────────────────────


class TestCompleteStory(_TestSetup):
    def test_complete_all_tasks_done(self):
        s = story_tool.create_story("Completable")
        t1 = bootstrap.add_task("Task 1")
        story_tool.add_task_to_story(s["id"], t1["id"])
        bootstrap.update_task_status(t1["id"], "done", notes="Task 1 notes")

        result = story_tool.complete_story(s["id"])
        assert result is not None
        assert "completion_notes" in result
        assert result["completion_notes"] is not None
        assert "Task 1" in result["completion_notes"]

    def test_complete_fails_if_tasks_not_done(self):
        s = story_tool.create_story("Not Ready")
        t = bootstrap.add_task("Pending")
        story_tool.add_task_to_story(s["id"], t["id"])

        assert story_tool.complete_story(s["id"]) is None

    def test_complete_nonexistent(self):
        assert story_tool.complete_story("STORY-999") is None


# ── Story Board State ──────────────────────────────────────────


class TestGetStoryBoardState(_TestSetup):
    def test_board_returns_columns(self):
        story_tool.create_story("Test")
        board = story_tool.get_story_board_state()
        for col in story_tool.STORY_COLUMNS:
            assert col in board

    def test_board_includes_progress(self):
        s = story_tool.create_story("With Progress")
        t = bootstrap.add_task("Child")
        story_tool.add_task_to_story(s["id"], t["id"])
        bootstrap.update_task_status(t["id"], "in_progress")

        board = story_tool.get_story_board_state()
        in_prog = board.get("in_progress", [])
        assert len(in_prog) == 1
        assert "progress" in in_prog[0]
        assert in_prog[0]["progress"]["percent"] == 0


class TestGetColumnStories(_TestSetup):
    def test_returns_stories_in_column(self):
        s = story_tool.create_story("Backlog Story")
        stories = story_tool.get_column_stories("backlog")
        assert any(st["id"] == s["id"] for st in stories)

    def test_empty_column(self):
        stories = story_tool.get_column_stories("done")
        assert stories == []
