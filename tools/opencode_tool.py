import os
import shutil
import subprocess
import time
import requests
from typing import Any, Dict, List, Optional

# Real event logging — do NOT shadow bootstrap.append_event with a print stub,
# or OpenCode activity never reaches the event log (the dashboard's most
# important subsystem goes blind).
from bootstrap import append_event

# Constants for polling and timeouts
_REQUEST_TIMEOUT = 60
_API_TIMEOUT = 30
_TASK_POLL_INTERVAL = 10
_MAX_TASK_WAIT_TIME = 1800
_STARTUP_TIMEOUT = 30
_DEFAULT_PORT = int(os.environ.get("OPENCODE_PORT", "4096"))
_PORT_SCAN_RANGE = 10
_SERVER_HOSTNAME = "127.0.0.1"
_OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")

# The port a health check last succeeded on, and any server we started.
_active_port: Optional[int] = None
_server_process: Optional[subprocess.Popen] = None

try:
    from bootstrap import BASE_DIR
except ImportError:
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _health_ok(port: int, timeout: float = 2) -> bool:
    try:
        resp = requests.get(f"http://{_SERVER_HOSTNAME}:{port}/global/health", timeout=timeout)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _discover_port() -> Optional[int]:
    """Find the port a live OpenCode server is on, caching the result."""
    global _active_port
    if _active_port and _health_ok(_active_port):
        return _active_port
    for port in range(_DEFAULT_PORT, _DEFAULT_PORT + _PORT_SCAN_RANGE):
        if _health_ok(port, timeout=1):
            _active_port = port
            return port
    _active_port = None
    return None


def is_server_running() -> bool:
    """Check if an OpenCode server is up and responding (any scanned port)."""
    return _discover_port() is not None


def is_installed() -> bool:
    """True only if the OpenCode binary is actually on PATH."""
    return shutil.which(_OPENCODE_BIN) is not None


def _get_base_url() -> str:
    """Base URL for the live server — the discovered port, not a fixed guess."""
    port = _active_port or _discover_port() or _DEFAULT_PORT
    return f"http://{_SERVER_HOSTNAME}:{port}"


def _api_post(endpoint: str, json_body: Dict[str, Any], timeout: int = _API_TIMEOUT, job_id: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
    url = f"{_get_base_url()}{endpoint}"
    params = {}
    if job_id: params["job_id"] = job_id
    if project_id: params["project_id"] = project_id
    try:
        resp = requests.post(url, json=json_body, params=params, timeout=timeout)
        return resp.json()
    except requests.Timeout:
        return {"error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}


def _api_get(endpoint: str, job_id: Optional[str] = None, project_id: Optional[str] = None, timeout: int = _API_TIMEOUT) -> Dict[str, Any]:
    url = f"{_get_base_url()}{endpoint}"
    params = {}
    if job_id: params["job_id"] = job_id
    if project_id: params["project_id"] = project_id
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        return resp.json()
    except requests.Timeout:
        return {"error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}

def _extract_output(msg_resp: Dict[str, Any]) -> str:
    parts = msg_resp.get("parts", [])
    if not parts:
        return msg_resp.get("content", msg_resp.get("text", ""))
    text_parts = []
    for part in parts:
        content = part.get("content", part.get("text", ""))
        if content:
            text_parts.append(str(content))
    return "\n".join(text_parts) if text_parts else ""

def _get_session_diffs(session_id: str, job_id: Optional[str] = None, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    diff_resp = _api_get(f"/session/{session_id}/diff", job_id=job_id, project_id=project_id)
    if "error" in diff_resp: return []
    return diff_resp if isinstance(diff_resp, list) else []

def get_session_status(session_id: str) -> Dict[str, Any]:
    return _api_get(f"/session/{session_id}")

def execute_task(
    task_description: str,
    system_prompt: str,
    workdir: Optional[str] = None,
    model: Optional[str] = None,
    agent: Optional[str] = None,
    interface_spec: Optional[str] = None,
    additional_context: Optional[str] = None,
    timeout: int = _REQUEST_TIMEOUT,
    job_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a developer task through the OpenCode server with robust polling.

    Creates a session, sends the task prompt, and polls for completion if the 
    initial request times out.
    """
    if not is_server_running():
        err = "OpenCode server is not running"
        append_event("tool:opencode", {"action": "execute_task", "success": False, "error": err})
        return {"success": False, "error": err}

    # Build the full prompt
    prompt_parts = [f"Task: {task_description}"]
    if interface_spec:
        prompt_parts.append(
            "INTERFACE CONTRACT:\n"
            "The following specification defines the contract your implementation must satisfy.\n"
            "You MUST implement code that satisfies this contract. Do NOT deviate from the defined interfaces.\n"
            "If you identify a need for an additional property or endpoint (an Interface Gap), stop and report the gap.\n\n"
            f"```\n{interface_spec}\n```"
        )
    if additional_context:
        prompt_parts.append(f"Additional context:\n{additional_context}")

    full_prompt = "\n\n".join(prompt_parts)

    # Create session
    session_title = task_description[:80]
    session_resp = _api_post(
        "/session",
        json_body={"title": session_title, "workdir": workdir or str(BASE_DIR)},
        job_id=job_id,
        project_id=project_id,
    )
    if "error" in session_resp:
        err = f"Failed to create session: {session_resp['error']}"
        append_event("tool:opencode", {"action": "execute_task", "success": False, "error": err})
        return {"success": False, "error": err}

    session_id = session_resp.get("id") or session_resp.get("sessionID")
    if not session_id:
        err = f"Session created but no ID returned: {session_resp}"
        append_event("tool:opencode", {"action": "execute_task", "success": False, "error": err})
        return {"success": False, "error": err}

    # Resolve the model: caller-provided "provider/model", else config default.
    provider_id, model_id = _resolve_model(model)

    # Build message payload
    message_body: Dict[str, Any] = {
        "parts": [{"type": "text", "text": full_prompt}],
        "system": system_prompt,
        "model": {"providerID": provider_id, "modelID": model_id},
    }
    if agent:
        message_body["agent"] = agent

    # Send message and wait for response
    msg_resp = _api_post(
        f"/session/{session_id}/message",
        json_body=message_body,
        timeout=timeout,
        job_id=job_id,
        project_id=project_id,
    )

    # --- Handle Timeout and Polling Logic ---
    if "error" in msg_resp and "timeout" in msg_resp["error"].lower():
        print(f"[OpenCode] Initial request timed out. Starting polling for session {session_id}...")
        start_poll = time.time()
        while (time.time() - start_poll) < _MAX_TASK_WAIT_TIME:
            status_resp = get_session_status(session_id)
            if "error" in status_resp:
                msg_resp = status_resp
                break
            
            state = str(status_resp.get("status", status_resp.get("state", "running"))).lower()
            
            if state in ("completed", "success", "done", "finished", "idle"):
                # If completed, we need to fetch the actual message content from history.
                # The session status endpoint usually only provides metadata.
                history = _api_get(f"/session/{session_id}/message")
                if isinstance(history, list) and len(history) > 0:
                    msg_resp = history[-1] # The last message in the history is our target response
                else:
                    # Fallback to status_resp if history is unavailable/malformed
                    msg_resp = status_resp
                break
            elif state in ("failed", "error"):
                msg_resp = status_resp
                break
            
            time.sleep(_TASK_POLL_INTERVAL)
        else:
            msg_resp = {"error": f"Task timed out after {_MAX_TASK_WAIT_TIME}s of polling."}

    if "error" in msg_resp:
        err = msg_resp["error"]
        append_event("tool:opencode", {"action": "execute_task", "success": False, "session_id": session_id, "error": err})
        return {"success": False, "session_id": session_id, "error": err}

    output = _extract_output(msg_resp)
    if not output and "content" in msg_resp:
        output = msg_resp["content"]

    diffs = _get_session_diffs(session_id, job_id=job_id, project_id=project_id)
    success = bool(output)
    append_event(
        "tool:opencode",
        {
            "action": "execute_task",
            "success": success,
            "session_id": session_id,
            "task": task_description[:200],
            "diffs_count": len(diffs),
        },
    )

    return {
        "success": success,
        "session_id": session_id,
        "output": output,
        "diffs": diffs,
    }

_DEFAULT_PROVIDER = os.environ.get("OPENCODE_PROVIDER", "lmstudio")
_DEFAULT_MODEL_ID = os.environ.get("OPENCODE_MODEL", "qwen/qwen3.6-27b")


def _resolve_model(model: Optional[str]) -> tuple:
    """Turn a "provider/model" string into (providerID, modelID).

    Falls back to the configured defaults when no model is given. Previously
    the `model` argument was accepted and silently ignored.
    """
    if model and "/" in model:
        provider, _, model_id = model.partition("/")
        return provider, model_id
    if model:
        return _DEFAULT_PROVIDER, model
    return _DEFAULT_PROVIDER, _DEFAULT_MODEL_ID


def start_server(port: Optional[int] = None, workdir: Optional[str] = None) -> Dict[str, Any]:
    """Start an OpenCode server, or fail loudly.

    If one is already running, reuse it. Otherwise, when the binary is
    installed, spawn `opencode serve` and wait for its health check. When it
    is NOT installed, return a real failure instead of pretending success —
    the previous stub returned success unconditionally, so every developer
    task then died with a misleading "server is not running".
    """
    global _server_process, _active_port

    running = _discover_port()
    if running:
        return {"success": True, "url": f"http://{_SERVER_HOSTNAME}:{running}", "reused": True}

    if not is_installed():
        return {
            "success": False,
            "error": (
                f"OpenCode binary '{_OPENCODE_BIN}' not found on PATH. Install it "
                "(or set OPENCODE_BIN), or start the server manually."
            ),
        }

    target_port = port or _DEFAULT_PORT
    try:
        _server_process = subprocess.Popen(
            [_OPENCODE_BIN, "serve", "--port", str(target_port)],
            cwd=workdir or str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        return {"success": False, "error": f"Failed to launch OpenCode: {e}"}

    deadline = time.time() + _STARTUP_TIMEOUT
    while time.time() < deadline:
        if _server_process.poll() is not None:
            return {"success": False, "error": f"OpenCode server exited during startup (code {_server_process.returncode})"}
        if _health_ok(target_port, timeout=1):
            _active_port = target_port
            append_event("tool:opencode", {"action": "start_server", "success": True, "port": target_port})
            return {"success": True, "url": f"http://{_SERVER_HOSTNAME}:{target_port}"}
        time.sleep(1)

    return {"success": False, "error": f"OpenCode server did not become healthy within {_STARTUP_TIMEOUT}s"}


def stop_server() -> Dict[str, Any]:
    """Stop a server we started (best effort)."""
    global _server_process, _active_port
    if _server_process and _server_process.poll() is None:
        _server_process.terminate()
        try:
            _server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_process.kill()
        _server_process = None
        _active_port = None
        return {"success": True, "stopped": True}
    return {"success": True, "already_stopped": True}
