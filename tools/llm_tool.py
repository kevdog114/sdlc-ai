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

import yaml
from bootstrap import append_event, add_task

DEFAULT_ENDPOINT = "http://localhost:4096/v1/chat/completions"
DEFAULT_TIMEOUT = 30

# Hermes config path: env var override, then default location
_hermes_config_path = Path(os.environ.get("SDLCAI_HERMES_CONFIG", str(Path.home() / ".hermes" / "config.yaml")))
HERMES_CONFIG = Path(_hermes_config_path)

def _get_hermes_config() -> Dict[str, Any]:
    """Helper to load Hermes configuration."""
    if not HERMES_CONFIG.exists():
        return {}
    try:
        with open(HERMES_CONFIG, 'r') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def query_llm(
    prompt: str,
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    model: str = "local",
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Send a prompt to an LLM endpoint and return the response.

    Supports LiteLLM for flexible model routing or standard requests for local endpoints/proxies.

    If api_key/base_url are not provided, it attempts to pull from Hermes configuration.

    Returns a dict with:
      - content: the assistant's text response (or error message)
      - success: bool
      - usage: optional token usage dict
      - error: present only on failure
    """
    # 1. Resolve API Key, Base URL, and Model from Hermes config if not explicitly provided
    config = _get_hermes_config()
    if not api_key or not base_url or model == "local":
        litellm_cfg = config.get('providers', {}).get('litellm', {})
        if not api_key:
            api_key = litellm_cfg.get('api_key')
        if not base_url:
            base_url = litellm_cfg.get('base_url')
        
        # If we have a proxy/litellm config, let's see if there is a default model to use instead of 'local'
        if (api_key and base_url) and model == "local":
            model = config.get('model', {}).get('default', 'local')

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

    # Test 2: LM Studio Direct (Bypassing LiteLLM logic)
    print("\nTesting LM Studio Direct (localhost:1234)...")
    lm_studio_res = query_llm(
        "Say hello in three words.",
        base_url="http://localhost:1234/v1",
        model="google/gemma-4-26b-a4b" # Exact model name
    )
    print(json.dumps(lm_studio_res, indent=2))

    # Test 3: Hermes Config (Should hit the 500 error again if still pointing to port 4000)
    print("\nTesting Hermes-LiteLLM config...")
    config = _get_hermes_config()
    l_cfg = config.get('providers', {}).get('litellm', {})
    if l_cfg.get('api_key') and l_cfg.get('base_url'):
        res = query_llm("Say hello in three words.")
        print(json.dumps(res, indent=2))
