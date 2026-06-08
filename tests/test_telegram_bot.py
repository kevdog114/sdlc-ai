"""Tests for the Telegram Bot integration."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.telegram_bot import (
    get_config,
    is_configured,
    send_message,
    send_clarification,
    TelegramBotPoller,
)


class TestGetConfig:
    def test_from_env_vars(self):
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'env-token', 'TELEGRAM_CHAT_ID': 'env-chat'}):
            cfg = get_config()
        assert cfg["token"] == "env-token"
        assert cfg["chat_id"] == "env-chat"

    def test_from_project_state(self):
        with patch.dict('os.environ', {}, clear=True):
            with patch('tools.telegram_bot.load_project_state', return_value={
                "project_config": {
                    "telegram_bot_token": "cfg-token",
                    "telegram_chat_id": "cfg-chat",
                }
            }):
                cfg = get_config()
        assert cfg["token"] == "cfg-token"
        assert cfg["chat_id"] == "cfg-chat"

    def test_empty_when_not_configured(self):
        with patch.dict('os.environ', {}, clear=True):
            with patch('tools.telegram_bot.load_project_state', return_value={"project_config": {}}):
                cfg = get_config()
        assert cfg["token"] == ""
        assert cfg["chat_id"] == ""


class TestIsConfigured:
    def test_true_when_configured(self):
        with patch('tools.telegram_bot.get_config', return_value={"token": "abc", "chat_id": "123"}):
            assert is_configured() is True

    def test_false_when_missing_token(self):
        with patch('tools.telegram_bot.get_config', return_value={"token": "", "chat_id": "123"}):
            assert is_configured() is False

    def test_false_when_missing_chat_id(self):
        with patch('tools.telegram_bot.get_config', return_value={"token": "abc", "chat_id": ""}):
            assert is_configured() is False

    def test_false_when_both_missing(self):
        with patch('tools.telegram_bot.get_config', return_value={"token": "", "chat_id": ""}):
            assert is_configured() is False


class TestSendMessage:
    def test_sends_message_successfully(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 42}}
        mock_response.raise_for_status = MagicMock()

        with patch('tools.telegram_bot.get_config', return_value={"token": "test-token", "chat_id": "test-chat"}):
            with patch('requests.post', return_value=mock_response) as mock_post:
                result = send_message("Hello, world!")

        assert result is not None
        assert result["message_id"] == 42
        mock_post.assert_called_once()

    def test_returns_none_on_api_error(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False}
        mock_response.raise_for_status = MagicMock()

        with patch('tools.telegram_bot.get_config', return_value={"token": "test-token", "chat_id": "test-chat"}):
            with patch('requests.post', return_value=mock_response):
                result = send_message("Hello")

        assert result is None

    def test_returns_none_when_not_configured(self):
        with patch('tools.telegram_bot.get_config', return_value={"token": "", "chat_id": ""}):
            result = send_message("Hello")
        assert result is None

    def test_returns_none_on_network_error(self):
        with patch('tools.telegram_bot.get_config', return_value={"token": "test-token", "chat_id": "test-chat"}):
            with patch('requests.post', side_effect=Exception("Network error")):
                result = send_message("Hello")
        assert result is None


class TestSendClarification:
    def test_sends_formatted_message(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 100}}
        mock_response.raise_for_status = MagicMock()

        with patch('tools.telegram_bot.get_config', return_value={"token": "t", "chat_id": "c"}):
            with patch('requests.post', return_value=mock_response) as mock_post:
                msg_id = send_clarification("What DB?", "Storage needed.", "CLR-0001")

        assert msg_id == 100
        # Verify the message contains the request ID and question
        call_kwargs = mock_post.call_args[1]
        text = call_kwargs["json"]["text"]
        assert "CLR-0001" in text
        assert "What DB?" in text
        assert "Storage needed" in text

    def test_returns_none_when_not_configured(self):
        with patch('tools.telegram_bot.get_config', return_value={"token": "", "chat_id": ""}):
            msg_id = send_clarification("Q?", "Ctx.", "CLR-0001")
        assert msg_id is None


class TestTelegramBotPoller:
    def test_start_does_nothing_when_not_configured(self):
        with patch('tools.telegram_bot.is_configured', return_value=False):
            poller = TelegramBotPoller()
            poller.start()
            assert poller._running is False

    def test_start_starts_thread_when_configured(self):
        with patch('tools.telegram_bot.is_configured', return_value=True):
            with patch('threading.Thread') as mock_thread:
                poller = TelegramBotPoller()
                poller.start()

        assert poller._running is True
        mock_thread.assert_called_once()

    def test_stop_joins_thread(self):
        mock_thread = MagicMock()
        poller = TelegramBotPoller()
        poller._running = True
        poller._thread = mock_thread
        poller.stop()
        mock_thread.join.assert_called_once_with(timeout=3)

    def test_set_on_answer_callback(self):
        poller = TelegramBotPoller()
        callback = lambda x: None
        poller.set_on_answer(callback)
        assert poller._on_answer is callback

    def test_process_update_with_reply(self):
        """Simulate receiving a reply to a clarification message."""
        update = {
            "update_id": 1,
            "message": {
                "message_id": 200,
                "chat": {"id": "12345"},
                "text": "Use PostgreSQL",
                "reply_to_message": {
                    "message_id": 100,
                    "text": "Clarification question",
                },
            },
        }

        mock_request = {"id": "CLR-0001", "question": "What DB?"}

        poller = TelegramBotPoller()
        callback_called = []

        def callback(req_id):
            callback_called.append(req_id)

        poller.set_on_answer(callback)

        with patch('tools.clarification_tool.get_request_by_telegram_message', return_value=mock_request):
            with patch('tools.clarification_tool.answer_request') as mock_answer:
                mock_answer.return_value = {"id": "CLR-0001", "status": "answered"}
                with patch('tools.telegram_bot.send_message'):
                    poller._process_update(update)

        mock_answer.assert_called_once_with("CLR-0001", "Use PostgreSQL", answered_by="telegram")
        assert len(callback_called) == 1
        assert callback_called[0] == "CLR-0001"

    def test_process_update_without_reply(self):
        """Messages that aren't replies should be ignored."""
        update = {
            "update_id": 2,
            "message": {
                "message_id": 201,
                "chat": {"id": "12345"},
                "text": "Just a message",
            },
        }

        poller = TelegramBotPoller()
        with patch('tools.clarification_tool.get_request_by_telegram_message') as mock_get:
            poller._process_update(update)

        mock_get.assert_not_called()

    def test_process_update_with_empty_text(self):
        """Messages without text should be ignored."""
        update = {
            "update_id": 3,
            "message": {
                "message_id": 202,
                "chat": {"id": "12345"},
                "reply_to_message": {"message_id": 100},
            },
        }

        poller = TelegramBotPoller()
        with patch('tools.clarification_tool.get_request_by_telegram_message') as mock_get:
            poller._process_update(update)

        mock_get.assert_not_called()
