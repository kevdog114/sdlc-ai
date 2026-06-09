"""OpenCode Tool — Manages OpenCode server and executes developer tasks via HTTP API.

Starts a headless `opencode serve` server and uses its REST API to:
  - Create sessions for each developer task
  - Send prompts with system context and interface contracts
  - Wait for task completion
  - Capture file diffs produced by the agent
"""

import json
import os
import signal
import subprocess
import sys
import time
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bootstrap import BASE_DIR, append_event

# ── Server Management ───────────────────────────────────────────

_DEFAULT_PORT = 4096
_STARTUP_TIMEOUT = 30  # seconds to wait for server readiness
_REQUEST_TIMEOUT = 120  # seconds for API calls (tasks can take a while)

_server_process: Optional[subprocess.Popen] = None
_server_port: Optional[int] = None
_server_hostname: str = "127.0.0.1"


def _sync_state() -> None:
    """Sync module-level connection state with environment variables if present."""
    global _server_port, _server_hostname
    env_host = os.environ.get("OPENCODE_HOST")
    env_port = os.environ.get("OPENCODE_PORT")

    if env_host:
        _server_hostname = env_host
    if env_port:
        try:
            _server_port = int(env_port)
        except ValueError:
            pass

def _get_base_url() -> Optional[str]:
    """Return the base URL for API calls, syncing state from environment first."""
    _sync_state()
    if _server_port is None:
        return None
    return f"http://{_server_hostname}:{_server_port}"


def _find_free_port(port: int = _DEFAULT_PORT) -> int:
    """Find an available TCP port, starting from the given port."""
    for attempt in range(port, port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((_server_hostname, attempt))
                return attempt
            except OSError:
                continue
    raise RuntimeError("Could not find a free port for OpenCode server")


def is_installed() -> bool:
    """Check if the `opencode` binary is available on PATH."""
    result = subprocess.run(
        ["which", "opencode"],
        capture_output=True,
    )
    return result.returncode == 0


def start_server(
    port: Optional[int] = None,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Start the headless OpenCode server.

    Returns a dict with success status and the server URL.
    """
    global _server_process, _server_port

    if _server_process is not None and _server_process.poll() is None:
        return {"success": True, "url": _get_base_url(), "already_running": True}

    if not is_installed():
        err = "OpenCode is not installed. Run: curl -fsSL https://opencode.ai/install | bash"
        append_event("tool:opencode", {"action": "start_server", "success": False, "error": err})
        return {"success": False, "error": err}

    port = port or _find_free_port()
    cwd = workdir or str(BASE_DIR)

    env = {**os.environ}
    cmd = [
        "opencode",
        "serve",
        "--port", str(port),
        "--hostname", _server_hostname,
    ]

    try:
        _server_process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        err = f"Failed to start OpenCode server: {e}"
        append_event("tool:opencode", {"action": "start_server", "success": False, "error": err})
        return {"success": False, "error": err}

    # Wait for the server to be ready
    _server_port = port
    base_url = _get_base_url()
    start_time = time.time()
    while time.time() - start_time < _STARTUP_TIMEOUT:
        try:
            resp = requests.get(f"{base_url}/global/health", timeout=3)
            if resp.status_code == 200:
                _server_port = port
                append_event(
                    "tool:opencode",
                    {"action": "start_server", "success": True, "port": port, "url": base_url},
                )
                return {"success": True, "url": base_url, "port": port}
        except requests.RequestException:
            pass
        time.sleep(0.5)

    # Timeout
    stop_server()
    err = f"OpenCode server did not become ready within {_STARTUP_TIMEOUT}s"
    append_event("tool:opencode", {"action": "start_server", "success": False, "error": err})
    return {"success": False, "error": err}


def stop_server() -> Dict[str, Any]:
    """Stop the running OpenCode server."""
    global _server_process, _server_port

    if _server_process is None:
        return {"success": True, "already_stopped": True}

    try:
        _server_process.send_signal(signal.SIGTERM)
        try:
            _server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server_process.kill()
            _server_process.wait(timeout=5)
    except Exception as e:
        append_event("tool:opencode", {"action": "stop_server", "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}
    finally:
        _server_process = None
        _server_port = None

    append_event("tool:opencode", {"action": "stop_server", "success": True})
    return {"success": True}


def is_server_running() -> bool:
    """Check if the OpenCode server is up and responding."""
    # First, try to use existing module state
    base_url = _get_base_url()
    if base_url:
        try:
            resp = requests.get(f"{base_url}/global/health", timeout=3)
            return resp.status_code == 200
        except requests.RequestException:
            pass

    # If module state is empty, try to discover the server by scanning default ports
    for port in range(_DEFAULT_PORT, _DEFAULT_PORT + 10):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(('127.0.0.1', port)) == 0:
                    # Port is open! Check if it's actually an HTTP server
                    try:
                        resp = requests.get(f"http://127.0.0.1:{port}/global/health", timeout=3)
                        if resp.status_code == 200:
                            # Update module state so future calls are fast
                            global _server_port, _server_hostname
                            _server_port = port
                            _server_hostname = "127.0.0.1"
                            return True
                    except requests.RequestException:
                        continue
        except Exception:
            continue
    return False


# ── Session / Task Execution ────────────────────────────────────


def _api_post(path: str, json_body: Optional[Dict] = None, timeout: int = _REQUEST_TIMEOUT) -> Dict[str, Any]:
    """Helper for POST requests to the OpenCode server."""
    base_url = _get_base_url()
    if not base_url:
        return {"error": "Server not running"}
    try:
        resp = requests.post(f"{base_url}{path}", json=json_body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": f"Request failed: {e}"}


def _api_get(path: str, params: Optional[Dict] = None, timeout: int = _REQUEST_TIMEOUT) -> Dict[str, Any]:
    """Helper for GET requests to the OpenCode server."""
    base_url = _get_base_url()
    if not base_url:
        return {"error": "Server not running"}
    try:
        resp = requests.get(f"{base_url}{path}", params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": f"Request failed: {e}"}


def execute_task(
    task_description: str,
    system_prompt: str,
    workdir: Optional[str] = None,
    model: Optional[str] = None,
    agent: Optional[str] = None,
    interface_spec: Optional[str] = None,
    additional_context: Optional[str] = None,
    timeout: int = _REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    """Execute a developer task through the OpenCode server.

    Creates a session, sends the task prompt, waits for completion,
    and captures the resulting file diffs.

    Args:
        task_description: The task to execute.
        system_prompt: System prompt for the agent (e.g., from developer role).
        workdir: Working directory for the task. Defaults to BASE_DIR.
        model: Model to use (provider/model format). If None, uses OpenCode default.
        agent: Agent to use. If None, uses OpenCode default.
        interface_spec: Optional interface specification contract to include.
        additional_context: Optional additional context for the task.
        timeout: Maximum time to wait for completion.

    Returns:
        Dict with keys: success, session_id, output, diffs, error
    """
    if not is_server_running():
        err = "OpenCode server is not running"
        append_event("tool:opencode", {"action": "execute_task", "success": False, "error": err})
        return {"success": False, "error": err}

    # Build the full prompt
    prompt_parts = [
        f"Task: {task_description}",
    ]

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
        json_body={"title": session_title},
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

    # Build message payload
    message_body: Dict[str, Any] = {
        "parts": [
            {"type": "text", "text": full_prompt}
        ],
        "system": system_prompt,
    }
    if model:
        parts = model.split("/", 1)
        if len(parts) == 2:
            message_body["model"] = {
                "providerID": parts[0],
                "modelID": parts[1],
            }
        else:
            message_body["model"] = parts[0]
    if agent:
        message_body["agent"] = agent

    # Send message and wait for response
    msg_resp = _api_post(
        f"/session/{session_id}/message",
        json_body=message_body,
        timeout=timeout,
    )

    if "error" in msg_resp:
        err = f"Task execution failed: {msg_resp['error']}"
        append_event("tool:opencode", {"action": "execute_task", "success": False, "session_id": session_id, "error": err})
        return {"success": False, "session_id": session_id, "error": err}

    # Extract the assistant response from message parts
    output = _extract_output(msg_resp)

    # Get file diffs
    diffs = _get_session_diffs(session_id)

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


def _extract_output(msg_resp: Dict[str, Any]) -> str:
    """Extract the text output from a message response.

    The response contains `info` (message metadata) and `parts` (message content parts).
    """
    parts = msg_resp.get("parts", [])
    if not parts:
        # Fallback: check top-level keys
        return msg_resp.get("content", msg_resp.get("text", ""))

    text_parts = []
    for part in parts:
        content = part.get("content", part.get("text", ""))
        if content:
            text_parts.append(str(content))

    return "\n".join(text_parts) if text_parts else ""


def _get_session_diffs(session_id: str) -> List[Dict[str, Any]]:
    """Get the list of file diffs produced in a session."""
    diff_resp = _api_get(f"/session/{session_id}/diff")
    if "error" in diff_resp:
        return []

    if isinstance(diff_resp, list):
        return diff_resp
    return []


def get_session_status(session_id: str) -> Dict[str, Any]:
    """Get the status of a specific session."""
    return _api_get(f"/session/{session_id}")


def abort_session(session_id: str) -> Dict[str, Any]:
    """Abort a running session."""
    return _api_post(f"/session/{session_id}/abort")


def list_sessions() -> List[Dict[str, Any]]:
    """List all OpenCode sessions."""
    resp = _api_get("/session")
    if "error" in resp:
        return []
    return resp if isinstance(resp, list) else []


def get_server_info() -> Dict[str, Any]:
    """Get server health and version info."""
    return _api_get("/global/health")


def get_project_info() -> Dict[str, Any]:
    """Get current project info from the server."""
    return _api_get("/project/current")
