"""Tests for the Interface Tool — contract-first specification management."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bootstrap import (
    BASE_DIR,
    STATE_DIR,
    INTERFACE_SPECS_DIR,
    load_project_state,
    save_project_state,
)
from tools.interface_tool import (
    ensure_specs_dir,
    generate_interface_spec,
    load_interface_spec,
    load_interface_spec_parsed,
    get_active_spec,
    set_active_spec,
    get_task_spec_id,
    update_interface_spec,
    validate_implementation_against_spec,
    list_specs,
    delete_spec,
)


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    """Provide isolated state directories for each test."""
    specs_dir = tmp_path / "state" / "interface_specs"
    specs_dir.mkdir(parents=True)

    state_file = tmp_path / "state" / "project_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    save_project_state({
        "version": "1.0.0",
        "interface_specs": {},
        "active_spec_id": None,
    })

    with patch('tools.interface_tool.INTERFACE_SPECS_DIR', specs_dir):
        with patch('bootstrap.STATE_FILE_PATH', state_file):
            with patch('tools.interface_tool.load_project_state', load_project_state):
                with patch('tools.interface_tool.save_project_state', save_project_state):
                    yield specs_dir


class TestEnsureSpecsDir:
    def test_creates_directory(self, clean_state):
        result = ensure_specs_dir()
        assert result.is_dir()

    def test_returns_existing_directory(self, clean_state):
        result = ensure_specs_dir()
        assert result == clean_state


class TestGenerateInterfaceSpec:
    def test_generates_spec_and_returns_id(self, clean_state):
        mock_yaml = """
openapi: "3.0.0"
info:
  title: Test API
  version: "1.0"
paths: {}
"""
        mock_response = {
            "success": True,
            "content": f"```yaml\n{mock_yaml}\n```",
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                spec_id = generate_interface_spec("Test goal", [{"task": "t1", "role": "developer"}])

        assert spec_id is not None
        assert spec_id.startswith("spec-")
        assert len(spec_id) == 13  # spec- + 8 hex chars

    def test_writes_spec_file(self, clean_state):
        mock_yaml = """
openapi: "3.0.0"
info:
  title: Test API
  version: "1.0"
paths: {}
"""
        mock_response = {
            "success": True,
            "content": mock_yaml,
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                spec_id = generate_interface_spec("Test goal", [{"task": "t1", "role": "developer"}])

        spec_file = clean_state / f"{spec_id}.yaml"
        assert spec_file.is_file()
        content = spec_file.read_text()
        parsed = yaml.safe_load(content)
        assert parsed["info"]["title"] == "Test API"

    def test_registers_in_project_state(self, clean_state):
        mock_yaml = """
openapi: "3.0.0"
info:
  title: Test API
  version: "1.0"
paths: {}
"""
        mock_response = {
            "success": True,
            "content": mock_yaml,
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                spec_id = generate_interface_spec("Test goal", [{"task": "t1", "role": "developer"}])

        state = load_project_state()
        assert spec_id in state["interface_specs"]
        assert state["interface_specs"][spec_id]["goal"] == "Test goal"
        assert state["interface_specs"][spec_id]["subtask_count"] == 1

    def test_returns_none_on_llm_failure(self, clean_state):
        mock_response = {
            "success": False,
            "error": "API error",
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                result = generate_interface_spec("Test goal", [{"task": "t1", "role": "developer"}])

        assert result is None

    def test_returns_none_on_invalid_yaml(self, clean_state):
        mock_response = {
            "success": True,
            "content": "not valid yaml: [[[" ,
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                result = generate_interface_spec("Test goal", [{"task": "t1", "role": "developer"}])

        assert result is None

    def test_cleans_markdown_fences(self, clean_state):
        mock_yaml = """
openapi: "3.0.0"
info:
  title: Test API
  version: "1.0"
paths: {}
"""
        mock_response = {
            "success": True,
            "content": f"```yaml\n{mock_yaml}\n```",
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                spec_id = generate_interface_spec("Test goal", [{"task": "t1", "role": "developer"}])

        content = load_interface_spec(spec_id)
        assert "```" not in content


class TestLoadInterfaceSpec:
    def test_loads_existing_spec(self, clean_state):
        spec_content = 'openapi: "3.0.0"\ninfo:\n  title: Test\n'
        spec_file = clean_state / "test-spec.yaml"
        spec_file.write_text(spec_content)

        result = load_interface_spec("test-spec")
        assert result == spec_content

    def test_returns_none_for_missing_spec(self, clean_state):
        result = load_interface_spec("nonexistent")
        assert result is None

    def test_parsed_returns_dict(self, clean_state):
        spec_content = 'openapi: "3.0.0"\ninfo:\n  title: Test\n'
        spec_file = clean_state / "test-spec.yaml"
        spec_file.write_text(spec_content)

        result = load_interface_spec_parsed("test-spec")
        assert isinstance(result, dict)
        assert result["info"]["title"] == "Test"

    def test_parsed_returns_none_for_invalid(self, clean_state):
        spec_file = clean_state / "bad-spec.yaml"
        spec_file.write_text("not: valid: yaml: [[[" )

        result = load_interface_spec_parsed("bad-spec")
        assert result is None


class TestActiveSpec:
    def test_get_active_spec_none_when_empty(self, clean_state):
        result = get_active_spec()
        assert result is None

    def test_get_active_spec_returns_latest(self, clean_state):
        state = load_project_state()
        state["interface_specs"] = {
            "spec-aaa": {"id": "spec-aaa", "generated_at": "2024-01-01T00:00:00Z"},
            "spec-bbb": {"id": "spec-bbb", "generated_at": "2024-01-02T00:00:00Z"},
        }
        save_project_state(state)

        result = get_active_spec()
        assert result == "spec-bbb"

    def test_set_active_spec(self, clean_state):
        set_active_spec("spec-xyz")
        state = load_project_state()
        assert state["active_spec_id"] == "spec-xyz"


class TestGetTaskSpecId:
    def test_returns_spec_id_from_task(self, clean_state):
        with patch('bootstrap.get_task', return_value={"id": 1, "interface_spec_id": "spec-123"}):
            result = get_task_spec_id(1)
        assert result == "spec-123"

    def test_returns_none_when_no_spec(self, clean_state):
        with patch('bootstrap.get_task', return_value={"id": 1, "interface_spec_id": None}):
            result = get_task_spec_id(1)
        assert result is None

    def test_returns_none_when_task_not_found(self, clean_state):
        with patch('bootstrap.get_task', return_value=None):
            result = get_task_spec_id(999)
        assert result is None


class TestUpdateInterfaceSpec:
    def test_updates_spec_on_gap(self, clean_state):
        original = 'openapi: "3.0.0"\npaths: {}\n'
        spec_file = clean_state / "spec-original.yaml"
        spec_file.write_text(original)

        state = load_project_state()
        state["interface_specs"] = {
            "spec-original": {
                "id": "spec-original",
                "goal": "Test",
                "generated_at": "2024-01-01T00:00:00Z",
            }
        }
        save_project_state(state)

        updated_yaml = 'openapi: "3.0.0"\npaths:\n  /new:\n    get:\n      summary: New endpoint\n'
        mock_response = {
            "success": True,
            "content": updated_yaml,
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                result = update_interface_spec("spec-original", "Need /new endpoint", "Test task")

        assert result is True
        content = load_interface_spec("spec-original")
        assert "/new" in content

    def test_returns_false_for_missing_spec(self, clean_state):
        with patch('tools.interface_tool.append_event'):
            result = update_interface_spec("nonexistent", "gap", "task")
        assert result is False


class TestValidateImplementationAgainstSpec:
    def test_validates_compliant_implementation(self, clean_state):
        spec_content = 'openapi: "3.0.0"\npaths:\n  /users:\n    get: {}\n'
        spec_file = clean_state / "spec-val.yaml"
        spec_file.write_text(spec_content)

        mock_response = {
            "success": True,
            "content": "COMPLIANT - Implementation matches the spec.",
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                result = validate_implementation_against_spec("spec-val", "Implemented /users GET endpoint.")

        assert result["success"] is True
        assert result["compliant"] is True

    def test_validates_non_compliant_implementation(self, clean_state):
        spec_content = 'openapi: "3.0.0"\npaths:\n  /users:\n    get: {}\n'
        spec_file = clean_state / "spec-val.yaml"
        spec_file.write_text(spec_content)

        mock_response = {
            "success": True,
            "content": "NON_COMPLIANT - Missing /users endpoint.",
        }

        with patch('tools.interface_tool.query_llm', return_value=mock_response):
            with patch('tools.interface_tool.append_event'):
                result = validate_implementation_against_spec("spec-val", "Did not implement anything.")

        assert result["success"] is True
        assert result["compliant"] is False

    def test_returns_error_for_missing_spec(self, clean_state):
        result = validate_implementation_against_spec("nonexistent", "notes")
        assert result["success"] is False
        assert result["compliant"] is False


class TestListAndDeleteSpecs:
    def test_list_specs_returns_all(self, clean_state):
        state = load_project_state()
        state["interface_specs"] = {
            "spec-1": {"id": "spec-1"},
            "spec-2": {"id": "spec-2"},
        }
        save_project_state(state)

        result = list_specs()
        assert len(result) == 2
        assert "spec-1" in [s["id"] for s in result]
        assert "spec-2" in [s["id"] for s in result]

    def test_delete_spec_removes_file_and_state(self, clean_state):
        spec_file = clean_state / "spec-del.yaml"
        spec_file.write_text("openapi: '3.0.0'\n")

        state = load_project_state()
        state["interface_specs"] = {"spec-del": {"id": "spec-del"}}
        save_project_state(state)

        with patch('tools.interface_tool.append_event'):
            result = delete_spec("spec-del")

        assert result is True
        assert not spec_file.is_file()
        state = load_project_state()
        assert "spec-del" not in state["interface_specs"]
