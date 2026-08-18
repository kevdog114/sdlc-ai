import requests
import json
from typing import Optional, Any
from urllib.parse import urlparse
from bootstrap import append_event


def _check_scheme(url: str) -> Optional[str]:
    """Only plain web schemes are allowed (no file://, ftp://, gopher://...)."""
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        return f"URL scheme '{scheme or 'none'}' not allowed (http/https only)"
    return None


def get(url: str, params: Optional[dict] = None) -> dict:
    """Sends a GET request to the specified URL."""
    scheme_error = _check_scheme(url)
    if scheme_error:
        append_event("tool:network", {"action": "get", "url": url, "success": False, "error": scheme_error})
        return {"success": False, "error": scheme_error}
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        # Try to parse as JSON, if not, return text
        try:
            content = response.json()
        except json.JSONDecodeError:
            content = response.text

        append_event("tool:network", {"action": "get", "url": url, "success": True})
        return {"success": True, "content": content}
    except Exception as e:
        append_event("tool:network", {"action": "get", "url": url, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}

def post(url: str, data: Optional[dict] = None, json_data: Optional[dict] = None) -> dict:
    """Sends a POST request to the specified URL."""
    scheme_error = _check_scheme(url)
    if scheme_error:
        append_event("tool:network", {"action": "post", "url": url, "success": False, "error": scheme_error})
        return {"success": False, "error": scheme_error}
    try:
        response = requests.post(url, data=data, json=json_data, timeout=10)
        response.raise_for_status()
        try:
            content = response.json()
        except json.JSONDecodeError:
            content = response.text

        append_event("tool:network", {"action": "post", "url": url, "success": True})
        return {"success": True, "content": content}
    except Exception as e:
        append_event("tool:network", {"action": "post", "url": url, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    print("[test] Testing GET request to JSONPlaceholder...")
    get_res = get("https://jsonplaceholder.typicode.com/todos/1")
    print(json.dumps(get_res, indent=2))

    print("\n[test] Testing POST request to JSONPlaceholder...")
    post_res = post("https://jsonplaceholder.typicode.com/posts", json_data={"title": "foo", "body": "bar", "userId": 1})
    print(json.dumps(post_res, indent=2))

    print("\n[test] Testing GET request to non-existent URL...")
    err_res = get("https://jsonplaceholder.typicode.com/nonexistent-page")
    print(json.dumps(err_res, indent=2))
