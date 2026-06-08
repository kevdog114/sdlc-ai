"""LLM Reasoning Tool — local or LiteLLM-based inference interface."""

import json
import os
from pathlib import Path
from typing import Optional, Dict, Any

import requests
try:
    import litellm
except ImportError:
    litellm = None

from bootstrap import append_event, add_task

DEFAULT_ENDPOINT = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen/qwen3.6-27b"
DEFAULT_TIMEOUT = 600

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
    prompt: str,
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
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

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    # 2. Use LiteLLM SDK if available and requested (and NOT hitting a direct local endpoint like LM Studio)
    # We check if the user is explicitly trying to use 'litellm' or if they provided a base_url that isn't localhost:1234
    is_lm_studio = base_url and "localhost:1234" in base_url

    if litellm and model != "local" and not is_lm_studio and api_key and base_url:
        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                temperature=temperature,
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )

            content = response.choices[0].message.content
            usage = getattr(response, 'usage', None)
            usage_dict = usage.__dict__ if hasattr(usage, '__dict__') else (dict(usage) if usage else None)

            append_event(
                "tool:llm_reasoning",
                {
                    "action": "query",
                    "prompt_len": len(prompt),
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
    }
    
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = requests.post(endpoint, headers=headers, data=json.dumps(payload), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage")

        append_event(
            "tool:llm_reasoning",
            {
                "action": "query",
                "prompt_len": len(prompt),
                "response_len": len(content),
                "success": True,
                "usage": usage,
            },
        )

        return {"content": content, "success": True, "usage": usage}

    except Exception as e:
        error_msg = f"HTTP error (endpoint={endpoint}): {str(e)}"
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
