import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dashboard_tool import (
    _get_task_overview,
    _get_recent_intelligence,
    _get_system_pulse,
    _get_git_status_section,
    generate_dashboard,
)


def _mock_registry(tasks=None):
    return {
        "version": "1.0.0",
        "tasks": tasks or [],
        "next_id": 1,
    }


# --- Task Overview Tests ---


class TestGetTaskOverview:
    def test_basic_overview(self):
        registry = _mock_registry([
            {"id": 1, "status": "done"},
            {"id": 2, "status": "done"},
            {"id": 3, "status": "pending"},
            {"id": 4, "status": "in_progress"},
        ])
        with patch('tools.dashboard_tool.load_json', return_value=registry):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_task_overview()
        assert result["success"] is True
        assert "Total tasks: 4" in result["output"]
        assert "Done:        2" in result["output"]
        assert "Pending:     1" in result["output"]
        assert "In Progress: 1" in result["output"]

    def test_empty_registry(self):
        registry = _mock_registry([])
        with patch('tools.dashboard_tool.load_json', return_value=registry):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_task_overview()
        assert result["success"] is True
        assert "Total tasks: 0" in result["output"]

    def test_missing_registry_file(self):
        with patch('tools.dashboard_tool.load_json', return_value={}):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_task_overview()
        assert result["success"] is True
        assert "Total tasks: 0" in result["output"]

    def test_load_json_raises_exception(self):
        with patch('tools.dashboard_tool.load_json', side_effect=Exception("disk error")):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_task_overview()
        assert result["success"] is False
        assert "Unable to load task registry" in result["output"]
        assert result["error"] == "disk error"

    def test_return_format(self):
        registry = _mock_registry([{"id": 1, "status": "done"}])
        with patch('tools.dashboard_tool.load_json', return_value=registry):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_task_overview()
        assert "success" in result
        assert "output" in result
        assert "error" in result

    def test_unknown_status_counts_as_pending(self):
        registry = _mock_registry([{"id": 1, "status": "cancelled"}])
        with patch('tools.dashboard_tool.load_json', return_value=registry):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_task_overview()
        assert "Total tasks: 1" in result["output"]
        assert "Done:        0" in result["output"]


# --- Recent Intelligence Tests ---


class TestGetRecentIntelligence:
    def test_no_insights(self):
        with patch('tools.dashboard_tool._all_insight_files', return_value=[]):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_recent_intelligence()
        assert result["success"] is True
        assert "No insights stored" in result["output"]

    def test_single_insight(self):
        mock_file = MagicMock(spec=Path)
        mock_file.__str__ = MagicMock(return_value="/fake/insight.md")

        with patch('tools.dashboard_tool._all_insight_files', return_value=[mock_file]):
            with patch('tools.dashboard_tool._parse_insight', return_value={
                "topic": "API Design",
                "summary": "Use RESTful endpoints.",
                "content": "Full content here.",
            }):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_recent_intelligence()
        assert result["success"] is True
        assert "[API Design]" in result["output"]
        assert "Use RESTful endpoints." in result["output"]

    def test_three_insights(self):
        mock_files = [MagicMock(spec=Path) for _ in range(3)]

        insights = [
            {"topic": "Topic A", "summary": "Summary A", "content": "A"},
            {"topic": "Topic B", "summary": "Summary B", "content": "B"},
            {"topic": "Topic C", "summary": "Summary C", "content": "C"},
        ]

        def parse_side_effect(f):
            idx = mock_files.index(f)
            return insights[idx]

        with patch('tools.dashboard_tool._all_insight_files', return_value=mock_files):
            with patch('tools.dashboard_tool._parse_insight', side_effect=parse_side_effect):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_recent_intelligence()
        assert result["success"] is True
        assert "[Topic A]" in result["output"]
        assert "[Topic B]" in result["output"]
        assert "[Topic C]" in result["output"]

    def test_more_than_three_shows_last_three(self):
        mock_files = [MagicMock(spec=Path) for _ in range(5)]

        insights = [
            {"topic": f"Topic {i}", "summary": f"Summary {i}", "content": f"C{i}"}
            for i in range(5)
        ]

        def parse_side_effect(f):
            idx = mock_files.index(f)
            return insights[idx]

        with patch('tools.dashboard_tool._all_insight_files', return_value=mock_files):
            with patch('tools.dashboard_tool._parse_insight', side_effect=parse_side_effect):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_recent_intelligence()
        assert "[Topic 2]" in result["output"]
        assert "[Topic 3]" in result["output"]
        assert "[Topic 4]" in result["output"]
        assert "[Topic 0]" not in result["output"]
        assert "[Topic 1]" not in result["output"]

    def test_parse_insight_returns_empty_dict(self):
        mock_file = MagicMock(spec=Path)

        with patch('tools.dashboard_tool._all_insight_files', return_value=[mock_file]):
            with patch('tools.dashboard_tool._parse_insight', return_value={}):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_recent_intelligence()
        assert result["success"] is True

    def test_exception_handling(self):
        with patch('tools.dashboard_tool._all_insight_files', side_effect=Exception("io error")):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_recent_intelligence()
        assert result["success"] is True
        assert "Unable to load knowledge base" in result["output"]

    def test_return_format(self):
        with patch('tools.dashboard_tool._all_insight_files', return_value=[]):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_recent_intelligence()
        assert "success" in result
        assert "output" in result
        assert "error" in result


# --- System Pulse Tests ---


class TestGetSystemPulse:
    def _write_event_log(self, events):
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False)
        for ev in events:
            tmp.write(json.dumps(ev) + "\n")
        tmp.close()
        return Path(tmp.name)

    def test_no_event_log_file(self):
        fake_path = Path("/nonexistent/event_log.jsonl")
        with patch('tools.dashboard_tool.EVENT_LOG_PATH', fake_path):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_system_pulse()
        assert result["success"] is True
        assert "No events recorded" in result["output"]

    def test_empty_event_log(self):
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False)
        tmp.close()
        fake_path = Path(tmp.name)

        with patch('tools.dashboard_tool.EVENT_LOG_PATH', fake_path):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_system_pulse()

        Path(tmp.name).unlink(missing_ok=True)
        assert result["success"] is True
        assert "No events recorded" in result["output"]

    def test_events_with_llm_success(self):
        events = [
            {"timestamp": "2026-01-01T00:00:00", "type": "tool:git_operation", "operation": "commit"},
            {"timestamp": "2026-01-01T00:01:00", "type": "tool:llm_reasoning", "action": "query"},
        ]
        log_path = self._write_event_log(events)

        with patch('tools.dashboard_tool.EVENT_LOG_PATH', log_path):
            with patch('tools.dashboard_tool.query_llm', return_value={
                "success": True,
                "content": "The system committed code and queried the LLM.",
            }):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_system_pulse()

        Path(log_path).unlink(missing_ok=True)
        assert result["success"] is True
        assert "The system committed code" in result["output"]
        assert "2 recent events analyzed" in result["output"]

    def test_events_with_llm_failure_fallback(self):
        events = [
            {"timestamp": "2026-01-01T00:00:00", "type": "tool:git_operation"},
        ]
        log_path = self._write_event_log(events)

        with patch('tools.dashboard_tool.EVENT_LOG_PATH', log_path):
            with patch('tools.dashboard_tool.query_llm', return_value={
                "success": False,
                "content": "",
                "error": "connection error",
            }):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_system_pulse()

        Path(log_path).unlink(missing_ok=True)
        assert result["success"] is True
        assert "tool:git_operation" in result["output"]
        assert "1 recent events analyzed" in result["output"]

    def test_more_than_10_events_takes_last_10(self):
        events = [
            {"timestamp": f"2026-01-01T00:{i:02d}:00", "type": f"event:{i}"}
            for i in range(15)
        ]
        log_path = self._write_event_log(events)

        with patch('tools.dashboard_tool.EVENT_LOG_PATH', log_path):
            with patch('tools.dashboard_tool.query_llm', return_value={
                "success": True,
                "content": "Summary of recent activity.",
            }):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_system_pulse()

        Path(log_path).unlink(missing_ok=True)
        assert result["success"] is True
        assert "10 recent events analyzed" in result["output"]

    def test_exception_handling(self):
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True
        fake_path.read_text.side_effect = Exception("read error")

        with patch('tools.dashboard_tool.EVENT_LOG_PATH', fake_path):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_system_pulse()
        assert result["success"] is True
        assert "Unable to analyze recent events" in result["output"]

    def test_return_format(self):
        fake_path = Path("/nonexistent/event_log.jsonl")
        with patch('tools.dashboard_tool.EVENT_LOG_PATH', fake_path):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_system_pulse()
        assert "success" in result
        assert "output" in result
        assert "error" in result


# --- Git Status Tests ---


class TestGetGitStatusSection:
    def test_clean_repo(self):
        with patch('tools.dashboard_tool.git_current_branch', return_value={
            "success": True,
            "output": "main\n",
            "error": None,
        }):
            with patch('tools.dashboard_tool.git_status', return_value={
                "success": True,
                "output": "## main\n",
                "error": None,
            }):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_git_status_section()
        assert result["success"] is True
        assert "Branch: main" in result["output"]
        assert "Status: clean" in result["output"]

    def test_dirty_repo(self):
        with patch('tools.dashboard_tool.git_current_branch', return_value={
            "success": True,
            "output": "feature/test\n",
            "error": None,
        }):
            with patch('tools.dashboard_tool.git_status', return_value={
                "success": True,
                "output": "## feature/test\n M tools/test.py\n?? new_file.py\n",
                "error": None,
            }):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_git_status_section()
        assert result["success"] is True
        assert "Branch: feature/test" in result["output"]
        assert "has uncommitted changes" in result["output"]

    def test_git_failure(self):
        with patch('tools.dashboard_tool.git_current_branch', return_value={
            "success": False,
            "output": "",
            "error": "not a git repo",
        }):
            with patch('tools.dashboard_tool.git_status', return_value={
                "success": False,
                "output": "",
                "error": "not a git repo",
            }):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_git_status_section()
        assert result["success"] is True
        assert "Branch: unknown" in result["output"]
        assert "unable to determine" in result["output"]

    def test_exception_handling(self):
        with patch('tools.dashboard_tool.git_current_branch', side_effect=Exception("git error")):
            with patch('tools.dashboard_tool.append_event'):
                result = _get_git_status_section()
        assert result["success"] is True
        assert "Unable to retrieve git status" in result["output"]

    def test_return_format(self):
        with patch('tools.dashboard_tool.git_current_branch', return_value={
            "success": True,
            "output": "main\n",
            "error": None,
        }):
            with patch('tools.dashboard_tool.git_status', return_value={
                "success": True,
                "output": "## main\n",
                "error": None,
            }):
                with patch('tools.dashboard_tool.append_event'):
                    result = _get_git_status_section()
        assert "success" in result
        assert "output" in result
        assert "error" in result


# --- Generate Dashboard Tests ---


class TestGenerateDashboard:
    def test_full_dashboard(self):
        with patch('tools.dashboard_tool._get_task_overview', return_value={
            "success": True,
            "output": "=== TASK OVERVIEW ===\n  Total tasks: 5",
            "error": None,
        }):
            with patch('tools.dashboard_tool._get_recent_intelligence', return_value={
                "success": True,
                "output": "=== RECENT INTELLIGENCE ===\n  No insights.",
                "error": None,
            }):
                with patch('tools.dashboard_tool._get_system_pulse', return_value={
                    "success": True,
                    "output": "=== SYSTEM PULSE ===\n  All quiet.",
                    "error": None,
                }):
                    with patch('tools.dashboard_tool._get_git_status_section', return_value={
                        "success": True,
                        "output": "=== GIT STATUS ===\n  Branch: main\n  Status: clean",
                        "error": None,
                    }):
                        with patch('tools.dashboard_tool.append_event'):
                            result = generate_dashboard()
        assert result["success"] is True
        assert "PROJECT BRIEFING" in result["output"]
        assert "TASK OVERVIEW" in result["output"]
        assert "RECENT INTELLIGENCE" in result["output"]
        assert "SYSTEM PULSE" in result["output"]
        assert "GIT STATUS" in result["output"]
        assert result["error"] is None

    def test_dashboard_with_section_errors(self):
        with patch('tools.dashboard_tool._get_task_overview', return_value={
            "success": True,
            "output": "=== TASK OVERVIEW ===\n  OK",
            "error": "partial error",
        }):
            with patch('tools.dashboard_tool._get_recent_intelligence', return_value={
                "success": True,
                "output": "=== RECENT INTELLIGENCE ===\n  OK",
                "error": None,
            }):
                with patch('tools.dashboard_tool._get_system_pulse', return_value={
                    "success": True,
                    "output": "=== SYSTEM PULSE ===\n  OK",
                    "error": None,
                }):
                    with patch('tools.dashboard_tool._get_git_status_section', return_value={
                        "success": True,
                        "output": "=== GIT STATUS ===\n  OK",
                        "error": None,
                    }):
                        with patch('tools.dashboard_tool.append_event'):
                            result = generate_dashboard()
        assert result["success"] is True
        assert "partial error" in result["error"]
        assert "PROJECT BRIEFING" in result["output"]

    def test_dashboard_total_exception(self):
        with patch('tools.dashboard_tool._get_task_overview', side_effect=Exception("fatal")):
            with patch('tools.dashboard_tool.append_event'):
                result = generate_dashboard()
        assert result["success"] is False
        assert result["output"] == ""
        assert "fatal" in result["error"]

    def test_return_format(self):
        with patch('tools.dashboard_tool._get_task_overview', return_value={
            "success": True,
            "output": "OK",
            "error": None,
        }):
            with patch('tools.dashboard_tool._get_recent_intelligence', return_value={
                "success": True,
                "output": "OK",
                "error": None,
            }):
                with patch('tools.dashboard_tool._get_system_pulse', return_value={
                    "success": True,
                    "output": "OK",
                    "error": None,
                }):
                    with patch('tools.dashboard_tool._get_git_status_section', return_value={
                        "success": True,
                        "output": "OK",
                        "error": None,
                    }):
                        with patch('tools.dashboard_tool.append_event'):
                            result = generate_dashboard()
        assert "success" in result
        assert "output" in result
        assert "error" in result
        assert isinstance(result["success"], bool)
        assert isinstance(result["output"], str)
