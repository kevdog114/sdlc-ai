"""LLM Reasoning Tool — local or LiteLLM-based inference interface."""

import json
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
try:
    import litellm
except ImportError:
    litellm = None

from bootstrap import append_event, add_task
from tools.llm_logger import log_llm_call

DEFAULT_ENDPOINT = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen/qwen3.6-27b"
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_TOKENS = 16000

# Local config path: env var override, then default location
_config_path = Path(os.environ.get("SDLCAI_CONFIG", str(Path(__file__).resolve().parent.parent / "config.yaml")))

def _get_local_config() -> Dict[str, Any]:
    """Helper to load local project configuration."""
    if not _config_path.exists():
        return {}
    try:
        import yaml
        with open(_config_path, 'r') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def query_llm(
    prompt: Optional[str] = None,
    messages: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Send a prompt to an LLM endpoint and return the response.

    Supports LiteLLM for flexible model routing or standard requests for local endpoints/proxies.

    If api_key/base_url are not provided, it attempts to pull from local configuration.

    Returns a dict with:
      - content: the assistant's text response (or error message)
      - success: bool
      - usage: optional token usage dict
      - error: present only on failure
    """
    # 1. Resolve API Key, Base URL, Model, and Timeout from local config if not explicitly provided
    config = _get_local_config()
    llm_cfg = config.get('llm', {})

    # Only use config defaults when the caller hasn't explicitly set a value
    if not api_key:
        api_key = llm_cfg.get('api_key')
    if not base_url:
        base_url = llm_cfg.get('base_url')
    if model == "local":
        model = llm_cfg.get('default_model', DEFAULT_MODEL)

    # Timeout is configurable via env var or config, but respects explicit argument
    timeout = int(os.environ.get("SDLCAI_LLM_TIMEOUT", timeout))
    if timeout == DEFAULT_TIMEOUT and 'timeout' in llm_cfg:
        timeout = int(llm_cfg['timeout'])

    # Use provided messages or construct them from prompt/system_prompt
    if messages is None:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if prompt:
            messages.append({"role": "user", "content": prompt})
    else:
        # If messages are provided, ensure the system_prompt is at index 0 if it's not already there
        if system_prompt and (not messages or messages[0].get("role") != "system"):
            messages.insert(0, {"role": "system", "content": system_prompt})

    # Build safe request payload for logging (redacts API key)
    log_request = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }

    # 2. Use LiteLLM SDK if available and requested (and NOT hitting a direct local endpoint like LM Studio)
    # We check if the user is explicitly trying to use 'litellm' or if they provided a base_url that isn't localhost:1234
    is_lm_studio = base_url and "localhost:1234" in base_url

    if litellm and model != "local" and not is_lm_studio and api_key and base_url:
        start = time.monotonic()
        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )

            content = response.choices[0].message.content
            usage = getattr(response, 'usage', None)
            usage_dict = usage.__dict__ if hasattr(usage, '__dict__') else (dict(usage) if usage else None)
            duration_ms = (time.monotonic() - start) * 1000

            log_response = {"content": content, "success": True, "usage": usage_dict}
            log_llm_call(log_request, log_response, duration_ms, model, base_url)

            append_event(
                "tool:llm_reasoning",
                {
                    "action": "query",
                    "prompt_len": len(messages[0]["content"]) if messages else 0,
                    "response_len": len(content),
                    "success": True,
                    "usage": usage_dict,
                },
            )

            return {
                "content": content,
                "success": True,
                "usage": usage_dict,
            }

        except Exception as e:
            error_msg = str(e)
            duration_ms = (time.monotonic() - start) * 1000
            log_response = {"success": False, "error": error_msg}
            log_llm_call(log_request, log_response, duration_ms, model, base_url)

            if "Provider NOT provided" in error_msg or "BadRequestError" in error_msg:
                pass # Fall through to direct HTTP request fallback
            else:
                append_event("tool:llm_reasoning", {"action": "query", "success": False, "error": error_msg})
                return {"content": "", "success": False, "error": error_msg}

    # 3. Direct HTTP request fallback (Most robust for local endpoints like LM Studio)
    endpoint = base_url if base_url else DEFAULT_ENDPOINT

    if not endpoint.endswith("/chat/completions") and not endpoint.endswith("/"):
         endpoint = f"{endpoint.rstrip('/')}/chat/completions"

    payload = {
        "model": model, # Passes the exact string provided (e.g., google/gemma...)
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    start = time.monotonic()
    try:
        resp = requests.post(endpoint, headers=headers, data=json.dumps(payload), timeout=timeout)
        
        if resp.status_code != 200:
            print(f"\n[DEBUG] LLM Error Details:")
            print(f"Endpoint: {endpoint}")
            print(f"Payload: {json.dumps(payload, indent=2)}")
            print(f"Status Code: {resp.status_code}")
            print(f"Response Body: {resp.text}")

        resp.raise_for_status()
        data = resp.json()
        duration_ms = (time.monotonic() - start) * 1000

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage")

        log_response = {"content": content, "success": True, "usage": usage}
        log_llm_call(log_request, log_response, duration_ms, model, endpoint)

        append_event(
            "tool:llm_reasoning",
            {
                "action": "query",
                "prompt_len": len(messages[0]["content"]) if messages else 0,
                "response_len": len(content),
                "success": True,
                "usage": usage,
            },
        )

        return {"content": content, "success": True, "usage": usage}

    except Exception as e:
        duration_ms = (time.monotonic() - start) * 1000
        error_msg = f"HTTP error (endpoint={endpoint}): {str(e)}"
        log_response = {"success": False, "error": error_msg}
        log_llm_call(log_request, log_response, duration_ms, model, endpoint)

        append_event("tool:llm_reasoning", {"action": "query", "success": False, "error": error_msg})
        return {"content": "", "success": False, "error": error_msg}


if __name__ == "__main__":
    # Test 1: Local default fallback
    print("Testing local...")
    result = query_llm("Say hello in three words.")
    print(json.dumps(result, indent=2))

    # Test 2: LM Studio Direct
    print("\nTesting LM Studio Direct (localhost:1234)...")
    lm_studio_res = query_llm(
        "Say hello in three words.",
        base_url="http://localhost:1234/v1",
        model="google/gemma-4-26b-a4b" # Exact model name
    )
    print(json.dumps(lm_studio_res, indent=2))
