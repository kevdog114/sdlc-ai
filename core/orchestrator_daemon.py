"""Agentic OS Orchestrator Daemon — Reactive Task Execution Engine."""

import time
import logging
import signal
import sys
import yaml
from pathlib import Path
from typing import Dict, Any

# Ensure we can import our own tools
import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bootstrap import append_event, load_project_state, save_project_state, TASK_REGISTRY_PATH, BASE_DIR
from tools.agent_manager import spawn_agent, get_job_status, list_active_agents
from tools.registry_tool import update_task_status, list_tasks
from tools.llm_tool import query_llm

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("OrchestratorDaemon")

class OrchestratorDaemon:
    MAX_CONCURRENT_AGENTS = 2

    def __init__(self, poll_interval: int = 5):
        self.poll_interval = poll_interval
        self.running = True
        # Track which task IDs are currently being handled by a running agent job
        self.active_task_to_job: Dict[int, str] = {}

    def stop(self, signum=None, frame=None):
        logger.info("Shutting down orchestrator daemon...")
        self.running = False

    def _get_available_role(self, task_description: str) -> str:
        """
        Select the best role for a task description using the LLM.
        Falls back to keyword matching if the LLM is unavailable.
        """
        roles_dir = BASE_DIR / "roles"
        role_descriptions = []
        available_roles = []

        for role_file in sorted(roles_dir.glob("*.yaml")):
            try:
                with open(role_file, "r") as f:
                    role = yaml.safe_load(f)
                available_roles.append(role["name"])
                role_descriptions.append(
                    f"- {role['name']}: {', '.join(role.get('capabilities', []))}"
                )
            except Exception:
                continue

        if not role_descriptions:
            return "developer"

        joined_roles = "\n".join(role_descriptions)
        joined_options = ", ".join(available_roles)
        prompt = (
            f"Available roles and their capabilities:\n"
            f"{joined_roles}\n\n"
            f"Task: {task_description}\n\n"
            f"Return ONLY the name of the single best role for this task. "
            f"Valid options: {joined_options}. "
            f"Default to 'developer' if unsure."
        )

        try:
            resp = query_llm(prompt, temperature=0.0, timeout=15)
            if resp["success"]:
                chosen = resp["content"].strip().lower().split()[0]
                if chosen in available_roles:
                    logger.info(f"LLM selected role '{chosen}' for task")
                    return chosen
        except Exception as e:
            logger.warning(f"LLM role selection failed ({e}), falling back to keyword matching")

        # Fallback: keyword matching
        desc_lower = task_description.lower()
        if "design" in desc_lower or "plan" in desc_lower or "architect" in desc_lower:
            return "architect"
        if "test" in desc_lower or "verify" in desc_lower or "qa" in desc_lower:
            return "qa"
        if "research" in desc_lower or "analyze" in desc_lower or "investigate" in desc_lower:
            return "researcher"
        return "developer"

    def _reconcile_active_jobs(self):
        """Check on agents currently running and update registry if they finished."""
        state = load_project_state()
        active_agents = state.get("active_agents", {})
        
        completed_job_ids = []

        for job_id, metadata in list(active_agents.items()):
            # 1. Check the actual process status via agent_manager
            status_res = get_job_status(job_id)
            
            if not status_res["success"]:
                logger.error(f"Failed to check job {job_id}: {status_res['error']}")
                continue

            job_status = status_res["status"]
            
            # 2. If the job has reached a terminal state, update the registry
            if job_status in ["completed", "failed", "finished_with_unknown_outcome"]:
                logger.info(f"Job {job_id} reached terminal state: {job_status}")
                
                # We need to find which task this job was working on.
                # In our current implementation, we'll have to track it or search logs.
                # For now, let's assume the agent_manager/registry_tool handles status updates 
                # during the run, but if it crashes, we use the job metadata.
                
                completed_job_ids.append(job_id)

        # Cleanup finished agents from active state
        for jid in completed_job_ids:
            if jid in active_agents:
                del active_agents[jid]
        
        if completed_job_ids:
            state["active_agents"] = active_agents
            save_project_state(state)

    def _count_running_agents(self, active_agents: dict) -> int:
        """Count agents whose underlying process is still alive."""
        running = 0
        for metadata in active_agents.values():
            pid = metadata.get("pid")
            if pid:
                try:
                    os.kill(pid, 0)
                    running += 1
                except OSError:
                    pass
        return running

    def _process_pending_tasks(self):
        """Look for pending tasks and spawn agents to handle them."""
        pending_tasks = list_tasks(status="pending")
        if not pending_tasks:
            return

        logger.info(f"Found {len(pending_tasks)} pending tasks.")
        
        state = load_project_state()
        active_agents = state.get("active_agents", {})
        # Map of job_id -> task_id to know what to update when the agent finishes
        # In a production system, this should be persistent in project_state.json

        running_count = self._count_running_agents(active_agents)
        if running_count >= self.MAX_CONCURRENT_AGENTS:
            logger.info(f"Concurrency limit reached ({running_count}/{self.MAX_CONCURRENT_AGENTS} running). Deferring {len(pending_tasks)} pending tasks.")
            return
        
        for task in pending_tasks:
            task_id = task["id"]
            description = task["description"]

            # Check if this task is already being worked on by an active job
            is_being_handled = any(
                metadata.get("goal") == description or 
                str(task_id) in metadata.get("log_file", "") # heuristic
                for metadata in active_agents.values()
            )

            if not is_being_handled:
                running_count = self._count_running_agents(active_agents)
                if running_count >= self.MAX_CONCURRENT_AGENTS:
                    logger.info(f"Concurrency limit reached mid-batch ({running_count}/{self.MAX_CONCURRENT_AGENTS}). Stopping spawn loop.")
                    break

                role = self._get_available_role(description)
                logger.info(f"Spawning agent '{role}' for task #{task_id}: {description}")
                
                spawn_res = spawn_agent(goal=description, persona=role)
                
                if spawn_res["success"]:
                    job_id = spawn_res["job_id"]
                    # Mark the task as in-progress immediately to prevent double-spawning
                    update_task_status(task_id, "in_progress", notes=f"Agent spawned (Job: {job_id})")
                    append_event("system:orchestrator", {"action": "spawned_for_task", "task_id": task_id, "job_id": job_id, "role": role})
                else:
                    logger.error(f"Failed to spawn agent for task #{task_id}: {spawn_res['error']}")

    def run(self):
        logger.info("Orchestrator Daemon started.")
        append_event("system:orchestrator", {"action": "daemon_start"})
        
        while self.running:
            try:
                self._reconcile_active_jobs()
                self._process_pending_tasks()
            except Exception as e:
                logger.error(f"Error in orchestration loop: {e}")
            
            time.sleep(self.poll_interval)

        append_event("system:orchestrator", {"action": "daemon_stop"})
        logger.info("Orchestrator Daemon stopped.")

if __name__ == "__main__":
    daemon = OrchestratorDaemon()
    signal.signal(signal.SIGINT, daemon.stop)
    signal.signal(signal.SIGTERM, daemon.stop)
    daemon.run()
