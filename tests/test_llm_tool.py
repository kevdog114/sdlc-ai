import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.llm_tool import query_llm, DEFAULT_ENDPOINT, DEFAULT_TIMEOUT


class TestQueryLlmSuccess:
    def test_successful_query(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Hello, world!"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        with patch('tools.llm_tool.requests.post', return_value=mock_response):
            with patch('tools.llm_tool.append_event'):
                result = query_llm("Say hello.")
        assert result["success"] is True
        assert result["content"] == "Hello, world!"
        assert result["usage"]["total_tokens"] == 15

    def test_query_with_system_prompt(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Response"}}],
        }

        with patch('tools.llm_tool.requests.post') as mock_post:
            with patch('tools.llm_tool.append_event'):
                query_llm("User prompt", system_prompt="You are helpful.")
            mock_post.assert_called_once()
            call_data = json.loads(mock_post.call_args[1]["data"])
            assert len(call_data["messages"]) == 2
            assert call_data["messages"][0]["role"] == "system"
            assert call_data["messages"][1]["role"] == "user"

    def test_query_default_endpoint(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "OK"}}],
        }

        with patch('tools.llm_tool.requests.post') as mock_post:
            with patch('tools.llm_tool.append_event'):
                query_llm("test")
            assert mock_post.call_args[0][0] == DEFAULT_ENDPOINT

    def test_query_custom_endpoint(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "OK"}}],
        }

        with patch('tools.llm_tool.requests.post') as mock_post:
            with patch('tools.llm_tool.append_event'):
                query_llm("test", endpoint="http://custom:8080/v1/chat")
            assert mock_post.call_args[0][0] == "http://custom:8080/v1/chat"

    def test_query_temperature(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "OK"}}],
        }

        with patch('tools.llm_tool.requests.post') as mock_post:
            with patch('tools.llm_tool.append_event'):
                query_llm("test", temperature=0.1)
            call_data = json.loads(mock_post.call_args[1]["data"])
            assert call_data["temperature"] == 0.1


class TestQueryLlmErrors:
    def test_connection_error(self):
        import requests
        with patch('tools.llm_tool.requests.post', side_effect=requests.exceptions.ConnectionError("refused")):
            with patch('tools.llm_tool.append_event') as mock_event:
                result = query_llm("test")
            assert result["success"] is False
            assert "error" in result
            assert "Connection failed" in result["error"]
            mock_event.assert_called_once()
            event_payload = mock_event.call_args[0][1]
            assert event_payload["success"] is False

    def test_timeout_error(self):
        import requests
        with patch('tools.llm_tool.requests.post', side_effect=requests.exceptions.Timeout("timed out")):
            with patch('tools.llm_tool.append_event') as mock_event:
                result = query_llm("test", timeout=5)
            assert result["success"] is False
            assert "error" in result
            assert "timed out" in result["error"].lower() or "timeout" in result["error"].lower()

    def test_http_error(self):
        import requests
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")
        with patch('tools.llm_tool.requests.post', return_value=mock_resp):
            with patch('tools.llm_tool.append_event'):
                result = query_llm("test")
            assert result["success"] is False
            assert "error" in result
            assert "HTTP error" in result["error"]

    def test_unexpected_error(self):
        with patch('tools.llm_tool.requests.post', side_effect=RuntimeError("unexpected")):
            with patch('tools.llm_tool.append_event'):
                result = query_llm("test")
            assert result["success"] is False
            assert "error" in result


class TestQueryLlmLocalEndpoint:
    def test_local_endpoint_returns_valid_json(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Local LLM response"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
        }

        with patch('tools.llm_tool.requests.post', return_value=mock_response) as mock_post:
            with patch('tools.llm_tool.append_event'):
                result = query_llm(
                    "What is 2+2?",
                    endpoint="http://localhost:4096/v1/chat/completions",
                )
            mock_post.assert_called_once()
            assert mock_post.call_args[0][0] == "http://localhost:4096/v1/chat/completions"
            assert result["success"] is True
            assert "content" in result
            assert isinstance(result["content"], str)
