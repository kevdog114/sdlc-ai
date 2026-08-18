"""Command Center Tool — start, stop, and monitor the live radar dashboard.

Manages the FastAPI pulse server as a background subprocess, providing
a simple API for the agent or user to control the observability dashboard.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from bootstrap import BASE_DIR, append_event

PID_FILE = BASE_DIR / "state" / "command_center.pid"
PORT = 8080
# Loopback by default; pass --host to expose (requires SDLCAI_API_TOKEN —
# the server refuses a non-local bind without one).
HOST = "127.0.0.1"

def get_host() -> str:
    """Get the host from CLI args or default."""
    for i, arg in enumerate(sys.argv):
        if arg == "--host" and i + 1 < len(sys.argv):
            return sys.argv[i+1]
    return HOST

def get_url() -> str:
    """Get the full URL."""
    return f"http://{get_host()}:{PORT}"

def _is_port_in_use(port: int, host: str = None) -> bool:
    """Check if a TCP port is currently in use."""
    target_host = host or get_host()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((target_host, port))
            return False
        except OSError:
            return True

def _read_pid() -> Optional[int]:
    """Read the stored PID from file."""
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            return None
    return None

def _write_pid(pid: int) -> None:
    """Write PID to file."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))

def _remove_pid() -> None:
    """Remove PID file."""
    if PID_FILE.exists():
        PID_FILE.unlink()

def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def start() -> Dict[str, Any]:
    """Start the command center pulse server as a background process.

    Returns:
        Dict with keys: success, url, pid, error
    """
    current_host = get_host()
    current_url = get_url()
    existing_pid = _read_pid()
    if existing_pid and _is_process_alive(existing_pid):
        msg = f"Server already running (PID {existing_pid}) at {current_url}"
        append_event("tool:command_center", {"action": "start", "success": False, "error": msg})
        return {"success": False, "url": current_url, "pid": existing_pid, "error": msg}

    if _is_port_in_use(PORT, current_host):
        msg = f"Port {PORT} is already in use on host {current_host}"
        append_event("tool:command_center", {"action": "start", "success": False, "error": msg})
        return {"success": False, "url": current_url, "pid": None, "error": msg}

    python = sys.executable or "python3"

    try:
        proc = subprocess.Popen(
            [python, "-c", f"""
import sys
sys.path.insert(0, {str(BASE_DIR)!r})
from tools.pulse_server import start_pulse_server
url = start_pulse_server(host={current_host!r}, port={PORT})
import time
while True:
    time.sleep(3600)
            """],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        _write_pid(proc.pid)
        time.sleep(1)

        if _is_process_alive(proc.pid):
            append_event("tool:command_center", {"action": "start", "success": True, "pid": proc.pid, "url": current_url})
            return {"success": True, "url": current_url, "pid": proc.pid, "error": None}
        else:
            msg = "Server process exited immediately"
            _remove_pid()
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            append_event("tool:command_center", {"action": "start", "success": False, "error": msg, "stderr": stderr})
            return {"success": False, "url": current_url, "pid": None, "error": f"{msg}: {stderr[:200]}"}

    except Exception as e:
        _remove_pid()
        append_event("tool:command_center", {"action": "start", "success": False, "error": str(e)})
        return {"success": False, "url": current_url, "pid": None, "error": str(e)}

def stop() -> Dict[str, Any]:
    """Stop the running command center server.

    Returns:
        Dict with keys: success, error
    """
    pid = _read_pid()
    if pid is None:
        append_event("tool:command_center", {"action": "stop", "success": True, "note": "no_pid"})
        return {"success": True, "error": None}

    if not _is_process_alive(pid):
        _remove_pid()
        append_event("tool:command_center", {"action": "stop", "success": True, "note": "stale_pid_cleaned"})
        return {"success": True, "error": None}

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            if not _is_process_alive(pid):
                break
            time.sleep(0.3)
        if _is_process_alive(pid):
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        _remove_pid()
        append_event("tool:command_center", {"action": "stop", "success": True, "pid": pid})
        return {"success": True, "error": None}
    except Exception as e:
        append_event("tool:command_center", {"action": "stop", "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}

def status() -> Dict[str, Any]:
    """Check the health of the command center server.

    Returns:
        Dict with keys: success, running, url, pid, healthy, error
    """
    current_host = get_host()
    current_url = get_url()
    pid = _read_pid()
    running = pid is not None and _is_process_alive(pid)

    if not running:
        if pid is not None:
            _remove_pid()
        append_event("tool:command_center", {"action": "status", "running": False})
        return {"success": True, "running": False, "url": current_url, "pid": None, "healthy": False, "error": None}

    healthy = False
    try:
        import urllib.request
        req = urllib.request.Request(f"{current_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            healthy = data.get("status") == "ok"
    except Exception:
        healthy = False

    append_event("tool:command_center", {"action": "status", "running": True, "healthy": healthy, "pid": pid})
    return {"success": True, "running": True, "url": current_url, "pid": pid, "healthy": healthy, "error": None}

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "start":
        result = start()
    elif action == "stop":
        result = stop()
    elif action == "status":
        result = status()
    else:
        print(f"Unknown action: {action}. Use: start, stop, status")
        sys.exit(1)
    print(json.dumps(result, indent=2, default=str))
