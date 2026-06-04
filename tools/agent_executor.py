"""Agent Executor Tool — Monitors registry and spawns subagents via subprocesses."""

import json
import os
import sys
import time
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add tools directory to path so we can import local modules
TOOLS_DIR = Path(__file__).parent.absolute()
if str(TOOLS_DIR) not in sys.path:
    sys.path.append(str(TOOLS_DIR))

try:
    from registry_tool import list_tasks, update_task_status, get_task_by_id
    from bootstrap import append_event
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)

def run_executor_loop(poll_interval: int = 10):
    """
    Continuous loop that checks for pending tasks and spawns subagents using subprocesses.
    This provides true project autonomy by running agents as independent processes.
    """
    print(f"[*] Agent Executor started. Polling every {poll_interval}s...")
    append_event("system:executor", {"action": "start_loop", "success": True})

    # Track active processes so we don't spawn the same task multiple times 
    # and can monitor them if needed.
    active_processes: Dict[int, subprocess.Popen] = {}

    try:
        while True:
            # 1. Clean up finished processes from our tracking dict
            finished_ids = [tid for tid, proc in active_processes.items() if proc.poll() is not None]
            for tid in finished_ids:
                print(f"[*] Process for Task #{tid} has exited.")
                del active_processes[tid]

            # 2. Check for pending tasks
            pending_tasks = list_tasks(status="pending")
            if pending_tasks:
                print(f"[*] Found {len(pending_tasks)} pending tasks. Spawning agents...")
                for task in pending_tasks:
                    task_id = task["id"]
                    description = task["description"]
                    persona = task.get("agent", "unassigned")

                    # Skip if we are already running this task ID
                    if task_id in active_processes:
                        continue

                    print(f"    [+] Spawning {persona} for Task #{task_id}: {description[:50]}...")
                    
                    try:
                        # Launch the worker as a background subprocess.
                        # This makes the agent a real, independent process within sdlc-ai.
                        process = subprocess.Popen(
                            [
                                sys.executable, 
                                str(TOOLS_DIR / "worker.py"),
                                "--task-id", str(task_id),
                                "--description", description,
                                "--persona", persona
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            # Set PYTHONPATH so the worker can find bootstrap/registry/llm tools
                            env={**os.environ, "PYTHONPATH": f"{TOOLS_DIR}:{sys.path[0]}"}
                        )
                        
                        active_processes[task_id] = process
                        
                        # Immediately mark as in_progress so the next poll doesn't duplicate
                        update_task_status(task_id, "in_progress")
                        
                        append_event("tool:orchestrator", {
                            "action": "agent_spawned",
                            "task_id": task_id,
                            "persona": persona,
                            "success": True
                        })

                    except Exception as e:
                        print(f"    [!] Failed to launch process for task #{task_id}: {e}")
                        append_event("tool:orchestrator", {
                            "action": "agent_spawn_failed",
                            "task_id": task_id,
                            "error": str(e),
                            "success": False
                        })
            
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n[*] Executor stopped by user. Terminating active agents...")
        for tid, proc in active_processes.items():
            proc.terminate()
        append_event("system:executor", {"action": "stop_loop", "success": True})
    except Exception as e:
        print(f"[!] Fatal Error in executor loop: {e}")
        append_event("system:executor", {"action": "loop_error", "error": str(e), "success": False})

if __name__ == "__main__":
    # For testing purposes, we run a single pass instead of an infinite loop 
    # unless specified.
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        run_executor_loop()
    else:
        print("[*] Running single-pass execution check...")
        pending = list_tasks(status="pending")
        print(f"[*] Found {len(pending)} pending tasks.")
        for t in pending:
            print(f"    - #{t['id']}: {t['description']}")
