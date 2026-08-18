import os
import time
import requests
from typing import Any, Dict, List, Optional

# Constants for polling and timeouts
_REQUEST_TIMEOUT = 60
_TASK_POLL_INTERVAL = 10
_MAX_TASK_WAIT_TIME = 1800
_STARTUP_TIMEOUT = 30
_DEFAULT_PORT = 4096
_SERVER_HOSTNAME = "127.0.0.1"

# BASE_DIR fallback if not imported from bootstrap
try:
    from bootstrap import BASE_DIR
except ImportError:
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def is_server_running() -> bool:
    """Check if an OpenCode server is up and responding."""
    base_url = f"http://{_SERVER_HOSTNAME}:{_DEFAULT_PORT}"
    try:
        resp = requests.get(f"{base_url}/global/health", timeout=3)
        return resp.status_code == 200
    except requests.RequestException:
        # Fallback scan if module state is lost
        for port in range(_DEFAULT_PORT, _DEFAULT_PORT + 10):
            try:
                with requests.get(f"http://127.0.0.1:{port}/global/health", timeout=1) as r:
                    if r.status_code == 200: return True
            except: continue
        return False

def _get_base_url() -> str:
    return f"http://{_SERVER_HOSTNAME}:{_DEFAULT_PORT}"

def append_event(tool_name: str, event_data: Dict[str, Any]):
    """Appends an event to the SDLC-AI registry."""
    # This is typically handled by core.runtime's append_event, 
    # but we need a local version for tool calls if they are direct.
    print(f"[{tool_name}] Event: {event_data}")

def _api_post(endpoint: str, json_body: Dict[str, Any], timeout: int = 30, job_id: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
    url = f"{_get_base_url()}{endpoint}"
    params = {}
    if job_id: params["job_id"] = job_id
    if project_id: params["project_id"] = project_id
    try:
        resp = requests.post(url, json=json_body, params=params, timeout=timeout)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

def _api_get(endpoint: str, job_id: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
    url = f"{_get_base_url()}{endpoint}"
    params = {}
    if job_id: params["job_id"] = job_id
    if project_id: params["project_id"] = project_id
    try:
        resp = requests.get(url, params=params)
        return resp.json()
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

    # --- HARDCODE MODEL HERE ---
    effective_model = "lmstudio/qwen/qwen3.6-27b"
    
    # Build message payload
    message_body: Dict[str, Any] = {
        "parts": [{"type": "text", "text": full_prompt}],
        "system": system_prompt,
        "model": {"providerID": "lmstudio", "modelID": "qwen/qwen3.6-27b"},
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

# Placeholder/Stub functions for required imports in core.runtime
def start_server(port: Optional[int] = None, workdir: Optional[str] = None) -> Dict[str, Any]:
    return {"success": True, "url": _get_base_url()}

def stop_server() -> Dict[str, Any]:
    return {"success": True}

def is_installed() -> bool:
    return True
