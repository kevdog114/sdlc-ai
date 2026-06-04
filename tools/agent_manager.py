import subprocess
import os
import json
import uuid
import signal
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

from bootstrap import append_event, load_project_state, save_project_state, AGENT_JOBS_DIR, BASE_DIR

# Directory to store logs and metadata for spawned agents
JOB_DIR = AGENT_JOBS_DIR
JOB_DIR.mkdir(parents=True, exist_ok=True)

def spawn_agent(goal: str, persona: str = "architect", api_url: Optional[str] = None, max_iterations: int = 10) -> dict:
    """
    Spawns a new agent process in the background to accomplish a goal.
    Returns a job_id that can be used to track progress.
    """
    job_id = str(uuid.uuid4())[:8]
    job_path = JOB_DIR / job_id
    job_path.mkdir()

    log_file = job_path / "output.log"
    meta_file = job_path / "metadata.json"

    # Construct the command
    import sys
    python_exe = sys.executable
    project_root = str(BASE_DIR)
    
    command = [
        python_exe,
        "-m", "core.runtime",
        "--goal", goal,
        "--persona", persona,
        "--max_iterations", str(max_iterations),
        "--job_id", job_id
    ]

    # Use environment variable or argument for API URL (Pulse Server)
    if api_url:
        command.extend(["--pulse_url", api_url])
    elif os.environ.get("PULSE_SERVER_URL"):
        command.extend(["--pulse_url", os.environ.get("PULSE_SERVER_URL")])
    else:
        # Default for local dev
        command.extend(["--pulse_url", "http://localhost:8081"])

    try:
        # Open log file outside of a 'with' block to ensure it stays open for the process
        f_out = open(log_file, "w")
        f_err = open(log_file, "a") 

        process = subprocess.Popen(
            command,
            cwd=project_root,
            stdout=f_out,
            stderr=f_err,
            env={**os.environ, "PYTHONPATH": project_root}
        )

        metadata = {
            "job_id": job_id,
            "goal": goal,
            "persona": persona,
            "pid": process.pid,
            "start_time": datetime.now().isoformat(),
            "status": "running",
            "log_file": str(log_file),
            "pulse_url": command[-2] if "--pulse_url" in command else "default"
        }

        with open(meta_file, "w") as f:
            json.dump(metadata, f, indent=2)

        # Update global project state with the new agent
        state = load_project_state()
        if "active_agents" not in state:
            state["active_agents"] = {}
        state["active_agents"][job_id] = metadata
        save_project_state(state)

        append_event("tool:agent_manager", {"action": "spawn", "job_id": job_id, "success": True, "persona": persona})
        return {"success": True, "job_id": job_id, "pid": process.pid}

    except Exception as e:
        error_msg = str(e)
        append_event("tool:agent_manager", {"action": "spawn", "success": False, "error": error_msg})
        return {"success": False, "error": error_msg}

def kill_agent(job_id: str) -> dict:
    """Terminates a running agent process."""
    state = load_project_state()
    agents = state.get("active_agents", {})
    
    if job_id not in agents:
        return {"success": False, "error": f"Job {job_id} not found in active agents."}

    pid = agents[job_id]["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
        # Update state
        del state["active_agents"][job_id]
        save_project_state(state)
        append_event("tool:agent_manager", {"action": "kill", "job_id": job_id, "success": True})
        return {"success": True, "message": f"Agent {job_id} terminated."}
    except ProcessLookupError:
        # PID already gone
        del state["active_agents"][job_id]
        save_project_state(state)
        return {"success": True, "message": "Process was already dead."}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_job_status(job_id: str) -> dict:
    """Checks the status of a spawned agent job."""
    state = load_project_state()
    agents = state.get("active_agents", {})
    
    if job_id in agents:
        metadata = agents[job_id]
    else:
        # Fallback to checking disk if not in active state (e.g., after restart)
        job_path = JOB_DIR / job_id
        meta_file = job_path / "metadata.json"
        if not meta_file.exists():
            return {"success": False, "error": f"Job {job_id} not found."}
        with open(meta_file, "r") as f:
            metadata = json.load(f)

    pid = metadata.get("pid")
    status = metadata.get("status", "unknown")

    # Check if the process is actually still running via PID
    try:
        os.kill(pid, 0)
        if status != "running": # If state was marked completed but PID exists, it's weird
            status = "running"
    except OSError:
        if status == "running":
            # It died. Let's determine if it succeeded or failed via logs.
            status = "completed_or_failed"

    if status == "completed_or_failed":
        log_file = JOB_DIR / job_id / "output.log"
        if log_file.exists():
            with open(log_file, "r") as f:
                content = f.read()
                if "FINAL RESPONSE" in content or "success" in content.lower():
                    status = "completed"
                elif "Error" in content or "Traceback" in content:
                    status = "failed"
                else:
                    status = "finished_with_unknown_outcome"

    # Sync status back to state if it changed
    if status != metadata.get("status"):
        metadata["status"] = status
        agents[job_id] = metadata
        save_project_state(state)

    return {"success": True, "status": status, "metadata": metadata}

def get_job_output(job_id: str) -> dict:
    """Retrieves the full log output of a spawned agent job."""
    log_file = JOB_DIR / job_id / "output.log"

    if not log_file.exists():
        return {"success": False, "error": f"No logs found for job {job_id}."}

    try:
        with open(log_file, "r") as f:
            content = f.read()
        return {"success": True, "output": content}
    except Exception as e:
        return {"success": False, "error": str(e)}

def list_active_agents() -> List[Dict[str, Any]]:
    """Returns a list of all currently managed active agents."""
    state = load_project_state()
    return list(state.get("active_agents", {}).values())

if __name__ == "__main__":
    # Simple test logic for manual execution
    print("[test] Spawning dummy agent...")
    res = spawn_agent("echo 'Hello from spawned agent'", "architect")
    print(res)
    if res["success"]:
        import time
        time.sleep(2)
        print("[test] Status:", get_job_status(res["job_id"]))
        print("[test] Output:", get_job_output(res["job_id"]))
