"""LLM Call Logger — persists every LLM request and response to llm_log/."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bootstrap import LLM_LOG_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_llm_call(
    request: Dict[str, Any],
    response: Dict[str, Any],
    duration_ms: float,
    model: str,
    endpoint: Optional[str] = None,
) -> str:
    """Log a single LLM request/response pair to a JSON file.

    Args:
        request: The request payload (messages, params, etc.).
        response: The response from the LLM.
        duration_ms: Wall-clock time in milliseconds.
        model: Model identifier.
        endpoint: Optional endpoint URL.

    Returns:
        The log file path.
    """
    LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)

    call_id = uuid.uuid4().hex[:12]
    timestamp = _now()

    entry = {
        "call_id": call_id,
        "timestamp": timestamp,
        "model": model,
        "endpoint": endpoint,
        "duration_ms": round(duration_ms, 2),
        "request": request,
        "response": response,
    }

    filename = f"{timestamp.replace(':', '-')}_{call_id}.json"
    filepath = LLM_LOG_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, default=str)

    return str(filepath)
