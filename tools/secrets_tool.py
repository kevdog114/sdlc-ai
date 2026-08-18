"""Secrets Tool — the single path to credentials, plus redaction helpers.

Implements the README's tool-gated secret access: components fetch secrets at
use time via get_secret(); values live in the environment or the gitignored
.secrets/ directory — never in the repo, the state store, or logs.

Resolution order for get_secret("llm_api_key"):
  1. env SDLCAI_SECRET_LLM_API_KEY
  2. env LLM_API_KEY
  3. .secrets/secrets.json  {"llm_api_key": "..."}  (repo root, gitignored)

redact() masks secret-bearing values in nested structures before they are
returned from APIs or written to logs.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

_SECRETS_FILE = Path(__file__).resolve().parent.parent / ".secrets" / "secrets.json"

_SECRET_NAME_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|credential|private[_-]?key|auth)",
    re.IGNORECASE,
)

REDACTED = "***redacted***"


def looks_secret(name: str) -> bool:
    """Heuristic: does this key/variable name refer to a credential?"""
    return bool(_SECRET_NAME_RE.search(name or ""))


def _read_secrets_file() -> dict:
    try:
        if _SECRETS_FILE.is_file():
            data = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def get_secret(key: str) -> Optional[str]:
    """Fetch a secret by name into volatile memory. Never log the value."""
    if not key:
        return None
    upper = key.upper()
    value = os.environ.get(f"SDLCAI_SECRET_{upper}") or os.environ.get(upper)
    if value:
        return value
    value = _read_secrets_file().get(key.lower())
    return value or None


def get_api_token() -> Optional[str]:
    """The dashboard/API auth token, when the operator has configured one."""
    return os.environ.get("SDLCAI_API_TOKEN") or get_secret("api_token")


def redact(obj: Any) -> Any:
    """Return a deep copy of obj with values under secret-looking keys masked."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if looks_secret(str(k)) and isinstance(v, (str, int, float)) and v != ""
                else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    return obj
