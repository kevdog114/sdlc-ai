"""Telegram Bot — sends clarification questions and receives human answers.

Uses the raw Telegram Bot API (no heavy dependencies beyond `requests`).
Runs a background polling thread alongside the Pulse Server.

Configuration (in order of precedence):
  - Environment variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  - Project state config: project_state.project_config.telegram_bot_token, telegram_chat_id
"""

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from bootstrap import append_event, load_project_state

logger = logging.getLogger("TelegramBot")

API_BASE = "https://api.telegram.org/bot{token}"
POLL_INTERVAL = 2.0
MAX_TIMEOUT = 30


def get_config() -> Dict[str, str]:
    """Resolve bot token and chat ID from env vars or project config."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or ""

    if not token or not chat_id:
        try:
            state = load_project_state()
            cfg = state.get("project_config", {})
            if not token:
                token = cfg.get("telegram_bot_token", "")
            if not chat_id:
                chat_id = cfg.get("telegram_chat_id", "")
        except Exception:
            pass

    return {"token": token, "chat_id": chat_id}


def is_configured() -> bool:
    """Return True if both token and chat ID are configured."""
    cfg = get_config()
    return bool(cfg["token"] and cfg["chat_id"])


def send_message(text: str, parse_mode: str = "Markdown") -> Optional[Dict[str, Any]]:
    """Send a text message to the configured chat.

    Returns the API response (which includes message_id) or None on failure.
    """
    cfg = get_config()
    if not cfg["token"] or not cfg["chat_id"]:
        logger.warning("Telegram not configured — cannot send message")
        return None

    url = f"{API_BASE.format(token=cfg['token'])}/sendMessage"
    payload = {
        "chat_id": cfg["chat_id"],
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        logger.error(f"Telegram API error: {data}")
        return None
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return None


def send_clarification(question: str, context: str, request_id: str) -> Optional[int]:
    """Send a clarification question to the user via Telegram.

    Returns the Telegram message_id if successful, None otherwise.

    The message includes the request_id so the bot can match replies.
    """
    text = (
        f"*Clarification Needed* \\[`{request_id}`\\]\n\n"
        f"*Question:* {_escape_markdown(question)}\n\n"
        f"*Context:* {_escape_markdown(context)}\n\n"
        f"_Reply to this message with your answer._"
    )
    result = send_message(text)
    if result:
        return result.get("message_id")
    return None


def _escape_markdown(text: str) -> str:
    """Escape special Markdown characters for Telegram."""
    special = r"_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


# ── Background Polling ──────────────────────────────────────────


class TelegramBotPoller:
    """Background thread that polls Telegram for replies to clarification messages.

    When a reply is detected, it stores the answer via clarification_tool.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_update_id = 0
        self._on_answer: Optional[Callable[[str], None]] = None

    def set_on_answer(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked with the request_id when an answer arrives.

        The callback can be used to trigger pipeline resume.
        """
        self._on_answer = callback

    def start(self) -> None:
        if self._running:
            return
        if not is_configured():
            logger.info("Telegram not configured — bot not started")
            return

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Telegram bot poller started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _poll_loop(self) -> None:
        cfg = get_config()
        url = f"{API_BASE.format(token=cfg['token'])}/getUpdates"

        while self._running:
            try:
                params: Dict[str, Any] = {
                    "timeout": MAX_TIMEOUT,
                    "allowed_updates": json.dumps(["message"]),
                }
                if self._last_update_id:
                    params["offset"] = self._last_update_id + 1

                resp = requests.get(url, params=params, timeout=MAX_TIMEOUT + 5)
                resp.raise_for_status()
                data = resp.json()

                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        self._process_update(update)
                        update_id = update.get("update_id", 0)
                        if update_id > self._last_update_id:
                            self._last_update_id = update_id

            except requests.Timeout:
                continue
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                time.sleep(POLL_INTERVAL)

    def _process_update(self, update: Dict[str, Any]) -> None:
        """Process a single Telegram update looking for replies to our messages."""
        message = update.get("message")
        if not message:
            return

        reply_to = message.get("reply_to_message")
        if not reply_to:
            return

        # Check if the replied-to message is one of our clarification requests
        replied_msg_id = reply_to.get("message_id")
        chat_id = str(message["chat"]["id"])
        text = message.get("text", "")

        if not text:
            return

        from tools.clarification_tool import (
            get_request_by_telegram_message,
            answer_request,
        )

        req = get_request_by_telegram_message(chat_id, replied_msg_id)
        if not req:
            return

        # Found a match — record the answer
        result = answer_request(req["id"], text, answered_by="telegram")
        if result:
            logger.info(f"Clarification {req['id']} answered via Telegram: {text[:80]}")
            # Send confirmation
            send_message(
                f"✅ Thanks! Your answer for *{req['id']}* has been recorded.",
            )
            append_event(
                "tool:telegram_bot",
                {
                    "action": "clarification_answered",
                    "request_id": req["id"],
                    "answer_preview": text[:200],
                },
            )
            if self._on_answer:
                self._on_answer(req["id"])


# Singleton instance
_poller = TelegramBotPoller()


def start_poller(on_answer: Optional[Callable[[str], None]] = None) -> None:
    """Start the background Telegram polling thread."""
    if on_answer:
        _poller.set_on_answer(on_answer)
    _poller.start()


def stop_poller() -> None:
    """Stop the background Telegram polling thread."""
    _poller.stop()
