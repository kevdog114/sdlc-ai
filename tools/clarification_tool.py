"""Clarification Tool — manage ambiguity resolution requests.

When the BA detects unclear requirements, a ClarificationRequest is created.
The pipeline pauses until all pending clarifications are answered.
Answers can come via Telegram or the Dashboard.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bootstrap import CLARIFICATION_PATH, append_event, load_json, save_json

CLARIFICATION_COLUMNS = ["pending", "answered", "cancelled"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_registry() -> Dict[str, Any]:
    return load_json(CLARIFICATION_PATH, {"version": "1.0.0", "requests": [], "next_id": 1})


def _save_registry(registry: Dict[str, Any]) -> None:
    save_json(CLARIFICATION_PATH, registry)


def create_request(
    project_id: str,
    question: str,
    context: str,
    phase: str = "ba_analysis",
    asked_via: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new clarification request.

    Args:
        project_id: The project this belongs to.
        question: The question needing human input.
        context: Surrounding context so the human can answer accurately.
        phase: Which pipeline phase raised this (ba_analysis, architect_review, etc.).
        asked_via: Channels used to ask (e.g. ['telegram', 'dashboard']).

    Returns:
        The created request dict.
    """
    registry = _load_registry()
    req_id = registry["next_id"]
    registry["next_id"] = req_id + 1

    request = {
        "id": f"CLR-{req_id:04d}",
        "project_id": project_id,
        "question": question,
        "context": context,
        "phase": phase,
        "status": "pending",
        "asked_via": asked_via or ["dashboard"],
        "telegram_chat_id": None,
        "telegram_message_id": None,
        "answer": None,
        "answered_by": None,
        "created_at": _now(),
        "answered_at": None,
    }
    registry["requests"].append(request)
    _save_registry(registry)

    append_event(
        "tool:clarification",
        {"action": "create", "request_id": request["id"], "project_id": project_id, "phase": phase},
    )
    return request


def get_request(request_id: str) -> Optional[Dict[str, Any]]:
    """Look up a clarification request by ID."""
    registry = _load_registry()
    for req in registry["requests"]:
        if req["id"] == request_id:
            return req
    return None


def list_requests(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    phase: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List clarification requests with optional filters."""
    registry = _load_registry()
    results = registry["requests"]

    if project_id:
        results = [r for r in results if r["project_id"] == project_id]
    if status:
        results = [r for r in results if r["status"] == status]
    if phase:
        results = [r for r in results if r["phase"] == phase]

    return results


def get_pending_for_project(project_id: str) -> List[Dict[str, Any]]:
    """Return all unanswered requests for a project."""
    return list_requests(project_id=project_id, status="pending")


def answer_request(
    request_id: str,
    answer: str,
    answered_by: str = "dashboard",
) -> Optional[Dict[str, Any]]:
    """Record a human's answer to a clarification request.

    Args:
        request_id: The request to answer.
        answer: The human's response.
        answered_by: Source of the answer ('telegram' or 'dashboard').

    Returns:
        The updated request, or None if not found.
    """
    registry = _load_registry()
    for req in registry["requests"]:
        if req["id"] == request_id:
            req["status"] = "answered"
            req["answer"] = answer
            req["answered_by"] = answered_by
            req["answered_at"] = _now()
            _save_registry(registry)

            append_event(
                "tool:clarification",
                {
                    "action": "answer",
                    "request_id": request_id,
                    "answered_by": answered_by,
                    "success": True,
                },
            )
            return req
    return None


def answer_all_pending(project_id: str, answer: str = "Proceeding with requirements as stated.") -> int:
    """Answer all pending requests for a project at once. Returns count of answered requests."""
    pending = get_pending_for_project(project_id)
    count = 0
    for req in pending:
        if answer_request(req["id"], answer, answered_by="turbo-mode"):
            count += 1
    return count


def cancel_request(request_id: str) -> bool:
    """Cancel a clarification request (e.g. if the question is no longer relevant)."""
    registry = _load_registry()
    for req in registry["requests"]:
        if req["id"] == request_id:
            req["status"] = "cancelled"
            _save_registry(registry)
            append_event(
                "tool:clarification",
                {"action": "cancel", "request_id": request_id},
            )
            return True
    return False


def set_telegram_metadata(
    request_id: str,
    chat_id: str,
    message_id: int,
) -> bool:
    """Associate a Telegram message with a clarification request.

    This allows the bot to match replies back to the original request.
    """
    registry = _load_registry()
    for req in registry["requests"]:
        if req["id"] == request_id:
            req["telegram_chat_id"] = chat_id
            req["telegram_message_id"] = message_id
            if "telegram" not in req.get("asked_via", []):
                req.setdefault("asked_via", []).append("telegram")
            _save_registry(registry)
            return True
    return False


def get_request_by_telegram_message(chat_id: str, message_id: int) -> Optional[Dict[str, Any]]:
    """Find a clarification request by its Telegram message IDs."""
    registry = _load_registry()
    for req in registry["requests"]:
        if (
            req.get("telegram_chat_id") == chat_id
            and req.get("telegram_message_id") == message_id
            and req["status"] == "pending"
        ):
            return req
    return None


def count_by_status(project_id: Optional[str] = None) -> Dict[str, int]:
    """Return counts of requests grouped by status."""
    requests = list_requests(project_id=project_id)
    counts: Dict[str, int] = {"pending": 0, "answered": 0, "cancelled": 0}
    for r in requests:
        status = r.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
    return counts


def has_pending(project_id: str) -> bool:
    """Return True if there are unanswered requests for a project."""
    return len(get_pending_for_project(project_id)) > 0
