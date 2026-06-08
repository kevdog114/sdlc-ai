"""Tests for the Clarification Tool."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.clarification_tool import (
    create_request,
    get_request,
    list_requests,
    get_pending_for_project,
    answer_request,
    cancel_request,
    set_telegram_metadata,
    get_request_by_telegram_message,
    count_by_status,
    has_pending,
    CLARIFICATION_PATH,
)


class TestCreateRequest:
    def test_creates_request_with_defaults(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock) as mock_path:
            mock_path.parent.mkdir = MagicMock()
            with patch('tools.clarification_tool.load_json', return_value={"version": "1.0.0", "requests": [], "next_id": 1}):
                with patch('tools.clarification_tool.save_json'):
                    with patch('tools.clarification_tool.append_event'):
                        req = create_request(
                            project_id="proj-test",
                            question="What database?",
                            context="The project needs data storage.",
                        )

        assert req["id"] == "CLR-0001"
        assert req["project_id"] == "proj-test"
        assert req["question"] == "What database?"
        assert req["context"] == "The project needs data storage."
        assert req["status"] == "pending"
        assert req["phase"] == "ba_analysis"
        assert req["asked_via"] == ["dashboard"]
        assert req["answer"] is None

    def test_creates_request_with_custom_phase(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"version": "1.0.0", "requests": [], "next_id": 1}):
                with patch('tools.clarification_tool.save_json'):
                    with patch('tools.clarification_tool.append_event'):
                        req = create_request(
                            project_id="proj-test",
                            question="Architecture question?",
                            context="Design decision needed.",
                            phase="architect_design",
                            asked_via=["telegram", "dashboard"],
                        )

        assert req["phase"] == "architect_design"
        assert "telegram" in req["asked_via"]

    def test_increments_id(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"version": "1.0.0", "requests": [], "next_id": 5}):
                with patch('tools.clarification_tool.save_json'):
                    with patch('tools.clarification_tool.append_event'):
                        req = create_request("proj-test", "Q?", "Ctx.")

        assert req["id"] == "CLR-0005"

    def test_logs_event(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"version": "1.0.0", "requests": [], "next_id": 1}):
                with patch('tools.clarification_tool.save_json'):
                    with patch('tools.clarification_tool.append_event') as mock_evt:
                        create_request("proj-test", "Q?", "Ctx.")

        assert mock_evt.call_args[0][0] == "tool:clarification"
        assert mock_evt.call_args[0][1]["action"] == "create"

    def test_return_format(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"version": "1.0.0", "requests": [], "next_id": 1}):
                with patch('tools.clarification_tool.save_json'):
                    with patch('tools.clarification_tool.append_event'):
                        req = create_request("proj-test", "Q?", "Ctx.")

        assert "id" in req
        assert "project_id" in req
        assert "question" in req
        assert "context" in req
        assert "status" in req
        assert "created_at" in req


class TestGetRequest:
    def test_finds_existing_request(self):
        requests = [
            {"id": "CLR-0001", "project_id": "p1", "question": "Q1", "status": "pending"},
            {"id": "CLR-0002", "project_id": "p1", "question": "Q2", "status": "answered"},
        ]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                req = get_request("CLR-0002")

        assert req is not None
        assert req["id"] == "CLR-0002"
        assert req["question"] == "Q2"

    def test_returns_none_for_missing(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": []}):
                req = get_request("CLR-9999")

        assert req is None


class TestListRequests:
    def setup_requests(self):
        return [
            {"id": "CLR-0001", "project_id": "p1", "question": "Q1", "status": "pending", "phase": "ba_analysis"},
            {"id": "CLR-0002", "project_id": "p1", "question": "Q2", "status": "answered", "phase": "ba_analysis"},
            {"id": "CLR-0003", "project_id": "p2", "question": "Q3", "status": "pending", "phase": "architect_design"},
        ]

    def test_lists_all(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": self.setup_requests()}):
                results = list_requests()
        assert len(results) == 3

    def test_filters_by_project(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": self.setup_requests()}):
                results = list_requests(project_id="p1")
        assert len(results) == 2

    def test_filters_by_status(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": self.setup_requests()}):
                results = list_requests(status="pending")
        assert len(results) == 2

    def test_filters_by_phase(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": self.setup_requests()}):
                results = list_requests(phase="architect_design")
        assert len(results) == 1

    def test_combined_filters(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": self.setup_requests()}):
                results = list_requests(project_id="p1", status="pending")
        assert len(results) == 1


class TestGetPendingForProject:
    def test_returns_only_pending(self):
        requests = [
            {"id": "CLR-0001", "project_id": "p1", "status": "pending"},
            {"id": "CLR-0002", "project_id": "p1", "status": "answered"},
            {"id": "CLR-0003", "project_id": "p2", "status": "pending"},
        ]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                results = get_pending_for_project("p1")

        assert len(results) == 1
        assert results[0]["id"] == "CLR-0001"

    def test_empty_when_all_answered(self):
        requests = [
            {"id": "CLR-0001", "project_id": "p1", "status": "answered"},
        ]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                results = get_pending_for_project("p1")
        assert len(results) == 0


class TestAnswerRequest:
    def test_answers_pending_request(self):
        requests = [{"id": "CLR-0001", "project_id": "p1", "question": "Q?", "status": "pending", "answer": None}]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                with patch('tools.clarification_tool.save_json') as mock_save:
                    with patch('tools.clarification_tool.append_event'):
                        result = answer_request("CLR-0001", "PostgreSQL", answered_by="dashboard")

        assert result is not None
        assert result["status"] == "answered"
        assert result["answer"] == "PostgreSQL"
        assert result["answered_by"] == "dashboard"

    def test_returns_none_for_missing(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": []}):
                with patch('tools.clarification_tool.append_event'):
                    result = answer_request("CLR-9999", "answer")
        assert result is None

    def test_telegram_answer_source(self):
        requests = [{"id": "CLR-0001", "project_id": "p1", "status": "pending"}]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                with patch('tools.clarification_tool.save_json'):
                    with patch('tools.clarification_tool.append_event'):
                        result = answer_request("CLR-0001", "Yes", answered_by="telegram")

        assert result["answered_by"] == "telegram"

    def test_logs_event(self):
        requests = [{"id": "CLR-0001", "project_id": "p1", "status": "pending"}]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                with patch('tools.clarification_tool.save_json'):
                    with patch('tools.clarification_tool.append_event') as mock_evt:
                        answer_request("CLR-0001", "answer")

        assert mock_evt.call_args[0][1]["action"] == "answer"
        assert mock_evt.call_args[0][1]["answered_by"] == "dashboard"


class TestCancelRequest:
    def test_cancels_request(self):
        requests = [{"id": "CLR-0001", "project_id": "p1", "status": "pending"}]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                with patch('tools.clarification_tool.save_json'):
                    with patch('tools.clarification_tool.append_event'):
                        result = cancel_request("CLR-0001")

        assert result is True

    def test_returns_false_for_missing(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": []}):
                with patch('tools.clarification_tool.append_event'):
                    result = cancel_request("CLR-9999")
        assert result is False


class TestTelegramMetadata:
    def test_sets_telegram_metadata(self):
        requests = [{"id": "CLR-0001", "asked_via": ["dashboard"]}]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                with patch('tools.clarification_tool.save_json'):
                    result = set_telegram_metadata("CLR-0001", "12345", 678)

        assert result is True
        assert requests[0]["telegram_chat_id"] == "12345"
        assert requests[0]["telegram_message_id"] == 678
        assert "telegram" in requests[0]["asked_via"]

    def test_get_by_telegram_message(self):
        requests = [
            {"id": "CLR-0001", "telegram_chat_id": "12345", "telegram_message_id": 678, "status": "pending"},
            {"id": "CLR-0002", "telegram_chat_id": "12345", "telegram_message_id": 679, "status": "answered"},
        ]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                result = get_request_by_telegram_message("12345", 678)

        assert result is not None
        assert result["id"] == "CLR-0001"

    def test_get_by_telegram_ignores_answered(self):
        requests = [
            {"id": "CLR-0002", "telegram_chat_id": "12345", "telegram_message_id": 679, "status": "answered"},
        ]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                result = get_request_by_telegram_message("12345", 679)
        assert result is None


class TestCountByStatus:
    def test_counts_correctly(self):
        requests = [
            {"id": "1", "project_id": "p1", "status": "pending"},
            {"id": "2", "project_id": "p1", "status": "pending"},
            {"id": "3", "project_id": "p1", "status": "answered"},
            {"id": "4", "project_id": "p2", "status": "pending"},
        ]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                counts = count_by_status()

        assert counts["pending"] == 3
        assert counts["answered"] == 1
        assert counts["cancelled"] == 0

    def test_filters_by_project(self):
        requests = [
            {"id": "1", "project_id": "p1", "status": "pending"},
            {"id": "2", "project_id": "p2", "status": "pending"},
        ]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                counts = count_by_status(project_id="p1")

        assert counts["pending"] == 1


class TestHasPending:
    def test_true_when_pending(self):
        requests = [{"id": "1", "project_id": "p1", "status": "pending"}]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                assert has_pending("p1") is True

    def test_false_when_all_answered(self):
        requests = [{"id": "1", "project_id": "p1", "status": "answered"}]
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": requests}):
                assert has_pending("p1") is False

    def test_false_when_no_requests(self):
        with patch('tools.clarification_tool.CLARIFICATION_PATH', new_callable=MagicMock):
            with patch('tools.clarification_tool.load_json', return_value={"requests": []}):
                assert has_pending("p1") is False
