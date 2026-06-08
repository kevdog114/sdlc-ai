"""Agent Runtime — Real LLM-powered ReAct reasoning loop.

Replaces the simulation with actual LLM queries, tool execution,
and observation feedback for autonomous task completion.
"""

import argparse
import json
import sys
import time
import yaml
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "tools"))

from llm_tool import query_llm
from bootstrap import append_event, load_project_state, save_project_state
import shell_executor
import file_manager

# --- Tool Registry ---
AVAILABLE_TOOLS = {
    "shell": {
        "func": shell_executor.execute_command,
        "desc": "Execute a shell command. Args: {\"command\": \"string\"}"
    },
    "file_read": {
        "func": file_manager.read_file,
        "desc": "Read the content of a file. Args: {\"path\": \"string\"}"
    },
    "file_write": {
        "func": file_manager.write_file,
        "desc": "Write content to a file (overwrites). Args: {\"path\": \"string\", \"content\": \"string\"}"
    },
    "dir_list": {
        "func": file_manager.list_dir,
        "desc": "List files in a directory. Args: {\"path\": \"string\"}"
    },
}

# Add opencode tools if available
try:
    from opencode_tool import is_server_running, start_server, execute_task
    AVAILABLE_TOOLS["opencode_check"] = {
        "func": is_server_running,
        "desc": "Check if OpenCode server is running. No args."
    }
    AVAILABLE_TOOLS["opencode_start"] = {
        "func": start_server,
        "desc": "Start the OpenCode server. Args: {\"port\": number, \"workdir\": \"string\"}"
    }
    AVAILABLE_TOOLS["opencode_task"] = {
        "func": execute_task,
        "desc": "Execute a task via OpenCode. Args: {\"task_description\": \"string\", \"system_prompt\": \"string\", \"interface_spec\": \"string\", \"additional_context\": \"string\"}"
    }
except ImportError:
    pass

# Add opencode tools if available
try:
    from opencode_tool import is_server_running, start_server, execute_task
    AVAILABLE_TOOLS["opencode_check"] = {
        "func": is_server_running,
        "desc": "Check if OpenCode server is running. No args."
    }
    AVAILABLE_TOOLS["opencode_start"] = {
        "func": start_server,
        "desc": "Start the OpenCode server. Args: {\"port\": number, \"workdir\": \"string\"}"
    }
    AVAILABLE_TOOLS["opencode_task"] = {
        "func": execute_task,
        "desc": "Execute a task via OpenCode. Args: {\"task_description\": \"string\", \"system_prompt\": \"string\", \"interface_spec\": \"string\", \"additional_context\": \"string\"}"
    }
except ImportError:
    pass

# Add opencode tools if available
try:
    from opencode_tool import is_server_running, start_server, execute_task as oc_execute_task
    AVAILABLE_TOOLS["opencode_check"] = {
        "func": is_server_running,
        "desc": "Check if OpenCode server is running. No args."
    }
    AVAILABLE_TOOLS["opencode_start"] = {
        "func": start_server,
        "desc": "Start the OpenCode server. Args: {\"port\": number, \"workdir\": \"string\"}"
    }
    AVAILABLE_TOOLS["opencode_run"] = {
        "func": oc_execute_task,
        "desc": "Run a task via OpenCode. Args: {\"task_description\": \"string\", \"system_prompt\": \"string\", \"interface_spec\": \"string\", \"additional_context\": \"string\"}"
    }
except ImportError:
    pass

ROLES_DIR = BASE_DIR / "roles"


def load_role(role_name: str) -> Optional[Dict[str, Any]]:
    """Load an agent role definition by name."""
    role_path = ROLES_DIR / f"{role_name}.yaml"
    if not role_path.is_file():
        return None
    try:
        return yaml.safe_load(role_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None


def build_system_prompt(role_data: Dict[str, Any], goal: str) -> str:
    """Construct the ReAct system prompt from role definition."""
    system_prompt = role_data.get("system_prompt", "You are an AI agent.")
    capabilities = role_data.get("capabilities", [])
    tool_descriptions = "\n".join(
        f"- {name}: {info['desc']}" for name, info in AVAILABLE_TOOLS.items()
    )

    return (
        f"{system_prompt}\n\n"
        f"YOUR GOAL: {goal}\n\n"
        f"CAPABILITIES: {', '.join(capabilities)}\n\n"
        f"AVAILABLE TOOLS:\n{tool_descriptions}\n\n"
        f"OPERATING PROTOCOL (ReAct):\n"
        f"For every turn, follow this exact format:\n\n"
        f"THOUGHT: [Your reasoning about what to do next.]\n"
        f"ACTION: [One of: {', '.join(AVAILABLE_TOOLS.keys())}]\n"
        f"ARGS: [JSON object with tool parameters]\n"
        f"[Wait for Observation]\n\n"
        f"OBSERVATION: [The result returned by the tool.]\n\n"
        f"Repeat until you have enough information. When finished:\n\n"
        f"FINAL ANSWER: [Your complete response to the goal.]\n\n"
        f"RULES:\n"
        f"1. Only one ACTION per turn.\n"
        f"2. If an action fails, explain why and try a different approach.\n"
        f"3. Verify your work using tools (e.g., read a file after writing it).\n"
        f"4. Your FINAL ANSWER must be the complete solution."
    )


def execute_tool(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool function with provided parameters."""
    if name not in AVAILABLE_TOOLS:
        return {"success": False, "error": f"Unknown tool: {name}"}

    try:
        result = AVAILABLE_TOOLS[name]["func"](**params)
        if not isinstance(result, dict):
            result = {"success": True, "output": str(result)}
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


def parse_llm_response(content: str) -> Dict[str, Any]:
    """Parse LLM response for ACTION/ARGS or FINAL ANSWER."""
    if "FINAL ANSWER:" in content:
        final_result = content.split("FINAL ANSWER:")[1].strip()
        return {"type": "final", "content": final_result}

    if "ACTION:" in content and "ARGS:" in content:
        try:
            lines = content.split("\n")
            action_line = next((l for l in lines if "ACTION:" in l), None)
            args_line = next((l for l in lines if "ARGS:" in l), None)

            if action_line and args_line:
                tool_name = action_line.replace("ACTION:", "").strip().strip("[]'\" ")
                args_json = args_line.replace("ARGS:", "").strip()
                params = json.loads(args_json)
                return {"type": "action", "tool": tool_name, "params": params}
        except (json.JSONDecodeError, StopIteration):
            pass

    return {"type": "thought", "content": content.strip()}


class AgentRuntime:
    def __init__(
        self,
        goal: str,
        persona: str,
        max_iterations: int,
        job_id: str,
        pulse_url: str,
        task_id: Optional[int] = None,
    ):
        self.goal = goal
        self.persona = persona
        self.max_iterations = max_iterations
        self.job_id = job_id
        self.pulse_url = pulse_url
        self.task_id = task_id
        self.current_step = 0
        self.is_running = True

        role_data = load_role(persona)
        if not role_data:
            role_data = {
                "name": persona,
                "system_prompt": f"You are a {persona} agent.",
                "capabilities": [],
            }
        self.system_prompt = build_system_prompt(role_data, goal)
        self.history: List[Dict[str, str]] = []

    def send_telemetry(self, state: str, detail: str = ""):
        """Send a heartbeat to the Pulse Server."""
        payload = {
            "agent_id": self.job_id,
            "persona": self.persona,
            "state": state,
            "goal": self.goal,
            "detail": detail,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            resp = requests.post(
                f"{self.pulse_url}/api/telemetry", json=payload, timeout=2
            )
            if resp.status_code != 200:
                print(f"[Runtime] Warning: Telemetry failed ({resp.status_code})")
        except Exception as e:
            print(f"[Runtime] Warning: Could not connect to Pulse Server: {e}")

    def run(self):
        """Execute the ReAct reasoning loop."""
        print(f"--- Agent [{self.job_id}] Starting ---")
        print(f"Goal: {self.goal}")
        print(f"Persona: {self.persona}")

        self.send_telemetry("initializing", f"Starting agent for goal: {self.goal[:50]}...")
        append_event(
            "system:runtime",
            {"action": "started", "job_id": self.job_id, "persona": self.persona, "goal": self.goal},
        )

        try:
            for turn in range(self.max_iterations):
                self.current_step += 1
                print(f"\n--- Turn {self.current_step}/{self.max_iterations} ---")

                # --- Think ---
                state = "thinking"
                self.send_telemetry(state, f"Reasoning step {self.current_step}")
                print(f"[Thinking] Analyzing current state...")

                messages = [{"role": "system", "content": self.system_prompt}]
                for msg in self.history:
                    messages.append(msg)

                prompt = (
                    "Continue your reasoning loop. If you have enough information "
                    "to complete the goal, provide a FINAL ANSWER. Otherwise, take "
                    "the next ACTION with ARGS."
                )

                response = query_llm(
                    prompt=prompt,
                    system_prompt=self.system_prompt,
                    temperature=0.0,
                    model="local",
                )

                if not response["success"]:
                    raise Exception(f"LLM query failed: {response.get('error', 'Unknown error')}")

                content = response["content"].strip()
                print(f"[LLM]: {content[:200]}...")
                self.history.append({"role": "assistant", "content": content})

                parsed = parse_llm_response(content)

                # --- Final Answer ---
                if parsed["type"] == "final":
                    final_result = parsed["content"]
                    print(f"\n[+] Goal achieved. Final answer: {final_result[:200]}...")
                    self.send_telemetry("completed", "Task finished successfully.")
                    append_event(
                        "system:runtime",
                        {
                            "action": "completed",
                            "job_id": self.job_id,
                            "success": True,
                            "result": final_result[:500],
                        },
                    )
                    self._finalize_task(final_result, success=True)
                    return

                # --- Execute Action ---
                if parsed["type"] == "action":
                    tool_name = parsed["tool"]
                    params = parsed["params"]
                    print(f"[Acting] Executing {tool_name}({params})...")

                    self.send_telemetry("acting", f"Executing tool: {tool_name}")
                    observation = execute_tool(tool_name, params)
                    obs_str = json.dumps(observation)
                    print(f"[Observation]: {obs_str[:300]}...")

                    self.history.append(
                        {"role": "user", "content": f"OBSERVATION: {obs_str}"}
                    )
                else:
                    # No action or final answer; nudge the agent
                    print("[!] No ACTION or FINAL ANSWER detected. Nudging agent.")
                    nudge = "Please use the proper format: either provide an ACTION/ARGS block or a FINAL ANSWER."
                    self.history.append({"role": "user", "content": nudge})

            # Max iterations reached without completion
            raise Exception("Exceeded maximum reasoning turns. Agent could not complete the goal.")

        except KeyboardInterrupt:
            print("\n[Runtime] Interrupted by user.")
            self.send_telemetry("interrupted", "Process killed.")
            append_event(
                "system:runtime",
                {"action": "interrupted", "job_id": self.job_id},
            )
            self._finalize_task("Interrupted by user.", success=False)
        except Exception as e:
            error_msg = str(e)
            print(f"\n[Runtime] Error: {error_msg}")
            self.send_telemetry("failed", error_msg)
            append_event(
                "system:runtime",
                {"action": "failed", "job_id": self.job_id, "error": error_msg},
            )
            self._finalize_task(error_msg, success=False)

    def _finalize_task(self, result: str, success: bool):
        """Update task status and project state upon completion."""
        if self.task_id:
            try:
                from registry_tool import update_task_status
                status = "done" if success else "failed"
                update_task_status(
                    self.task_id, status, notes=f"Runtime result: {result[:500]}"
                )
            except ImportError:
                pass

        state = load_project_state()
        if self.job_id in state.get("active_agents", {}):
            agent = state["active_agents"][self.job_id]
            agent["status"] = "completed" if success else "failed"
            agent["result"] = result[:500]
            agent["end_time"] = datetime.now().isoformat()
            save_project_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SDLC-AI Agent Runtime")
    parser.add_argument("--goal", required=True, help="Task goal description")
    parser.add_argument("--persona", default="architect", help="Agent persona/role")
    parser.add_argument("--max_iterations", type=int, default=10, help="Max reasoning turns")
    parser.add_argument("--job_id", required=True, help="Unique job identifier")
    parser.add_argument("--pulse_url", default="http://localhost:8081", help="Pulse Server URL")
    parser.add_argument("--task_id", type=int, default=None, help="Registry task ID")

    args = parser.parse_args()

    runtime = AgentRuntime(
        goal=args.goal,
        persona=args.persona,
        max_iterations=args.max_iterations,
        job_id=args.job_id,
        pulse_url=args.pulse_url,
        task_id=args.task_id,
    )
    runtime.run()
