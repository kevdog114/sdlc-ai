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

# Ensure we can import our own tools
import os
from bootstrap import BASE_DIR
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "tools"))

from llm_tool import query_llm
from bootstrap import append_event, load_project_state, save_project_state
import shell_executor
import file_manager
from registry_tool import get_task_by_id
from agent_logger import log_agent_start, log_agent_step, log_agent_complete, log_opencode_task

# --- Tool Registry ---
try:
    import shell_executor
    import file_manager
    from registry_tool import list_tasks, get_task_by_id, update_task_status, create_new_task
    from knowledge_tool import store_insight, query_knowledge, list_topics
    from agent_state import write_memo, read_memos, clear_memo

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
        "registry_list_tasks": {
            "func": list_tasks,
            "desc": "List all tasks, optionally filtered by status. Args: {\"status\": \"string\"}"
        },
        "registry_get_task": {
            "func": get_task_by_id,
            "desc": "Retrieve a single task by its integer ID. Args: {\"task_id\": number}"
        },
        "registry_update_task": {
            "func": update_task_status,
            "desc": "Update the status of an existing task. Args: {\"task_id\": number, \"new_status\": \"string\", \"notes\": \"string\"}"
        },
        "registry_create_task": {
            "func": create_new_task,
            "desc": "Create a new task. Args: {\"description\": \"string\", \"agent\": \"string\", \"story_id\": \"string\", \"project_id\": \"string\"}"
        },
        "knowledge_store": {
            "func": store_insight,
            "desc": "Store a structured insight into the local knowledge repository. Args: {\"topic\": \"string\", \"content\": \"string\"}"
        },
        "knowledge_query": {
            "func": query_knowledge,
            "desc": "Search the knowledge repository. Args: {\"query\": \"string\"}"
        },
        "knowledge_list_topics": {
            "func": list_topics,
            "desc": "List all unique topics in the knowledge base. No args."
        },
        "agent_write_memo": {
            "func": write_memo,
            "desc": "Write a memo from one agent to another. Args: {\"sender\": \"string\", \"receiver\": \"string\", \"subject\": \"string\", \"content\": \"string\"}"
        },
        "agent_read_memos": {
            "func": read_memos,
            "desc": "Read all pending memos addressed to a specific agent. Args: {\"receiver\": \"string\"}"
        },
        "agent_clear_memo": {
            "func": clear_memo,
            "desc": "Deletes a memo after it has been processed. Args: {\"memo_id\": \"string\"}"
        }
    }

    # Add opencode tools if available
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
        "desc": "Execute a task via OpenCode. Args: {\"task_description\": \"string\", \"system_prompt\": \"string\", \"interface_spec\": \"string\", \"additional_context\": \"string\", \"model\": \"provider/model\"}"
    }
    AVAILABLE_TOOLS["opencode_run"] = {
        "func": execute_task,
        "desc": "Run a task via OpenCode. Args: {\"task_description\": \"string\", \"system_prompt\": \"string\", \"interface_spec\": \"string\", \"additional_context\": \"string\"}"
    }

except ImportError as e:
    print(f"[Runtime] Error loading tools: {e}")
    AVAILABLE_TOOLS = {}

# --- Path Discovery --------------------------------------------
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


def build_system_prompt(role_data: Dict[str, Any], goal: str, project_path: Optional[str] = None) -> str:
    """Construct the ReAct system prompt from role definition."""
    base_prompt = role_data.get("system_prompt", "You are an AI agent.")
    capabilities = role_data.get("capabilities", [])
    tool_descriptions = "\n".join(
        f"- {name}: {info['desc']}" for name, info in AVAILABLE_TOOLS.items()
    )

    prompt_sections = [base_prompt]

    # Inject Cognitive Profile
    if "cognitive_profile" in role_data:
        cp = role_data["cognitive_profile"]
        cp_text = "\nCOGNITIVE PROFILE:\n"
        for key, value in cp.items():
            formatted_key = key.replace("_", " ").title()
            if isinstance(value, list):
                val_str = ", ".join(value)
            else:
                val_str = str(value)
            cp_text += f"- {formatted_key}: {val_str}\n"
        prompt_sections.append(cp_text)

    # Inject Behavioral Instructions
    if "behavioral_instructions" in role_data:
        bi = role_data["behavioral_instructions"]
        bi_text = "\nBEHAVIORAL INSTRUCTIONS:\n"
        if "rules" in bi:
            bi_text += "\nRULES:\n" + "\n".join(f"- {rule}" for rule in bi["rules"]) + "\n"
        if "preferred_format" in bi:
            bi_text += f"PREFERRED FORMAT: {bi['preferred_format']}\n"
        prompt_sections.append(bi_text)

    # Inject Operational Constraints
    if "operational_constraints" in role_data:
        oc = role_data["operational_constraints"]
        oc_text = "\nOPERATIONAL CONSTRAINTS:\n"
        if "allowed_tools" in oc:
            oc_text += f"- ALLOWED TOOLS: {', '.join(oc['allowed_tools'])}\n"
        if "max_loop_depth" in oc:
            oc_text += f"- MAX LOOP DEPTH: {oc['max_loop_depth']}\n"
        prompt_sections.append(oc_text)

    # Add Goal and Protocol
    prompt_sections.extend([
        f"\nYOUR GOAL: {goal}\n",
    ])

    if project_path:
        prompt_sections.append(f"CURRENT WORKING PROJECT FOLDER: {project_path}")

    prompt_sections.extend([
        f"\nCAPABILITIES: {', '.join(capabilities)}\n",
        f"AVAILABLE TOOLS:\n{tool_descriptions}\n",
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
        #f"3. Verify your work using tools (e.g., read a file after writing it).\n"
        f"3. Your FINAL ANSWER must be the complete solution."
    ])

    return "\n".join(prompt_sections)


def execute_tool(name: str, params: Dict[str, Any], context: Optional[Dict] = None) -> Dict[str, Any]:
    """Execute a tool function with provided parameters."""
    if name not in AVAILABLE_TOOLS:
        return {"success": False, "error": f"Unknown tool: {name}"}

    # Enforcement of allowed tools via context
    allowed_tools = context.get("allowed_tools") if context else None
    if allowed_tools is not None and name not in allowed_tools:
        return {"success": False, "error": f"Access Denied: Tool '{name}' is not in your allowed list ({', '.join(allowed_tools)})."}

    # Automatically inject workdir if it's an opencode tool and context provides a project path
    if context and "project_path" in context:
        proj_path = context["project_path"]
        if name in ("opencode_task", "opencode_run"):
            # If the agent provided a workdir, respect it. Otherwise, use the project path.
            if "workdir" not in params:
                params["workdir"] = proj_path

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
        project_id: Optional[str] = None,
    ):
        self.goal = goal
        self.persona = persona
        self.max_iterations = max_iterations
        self.job_id = job_id
        self.pulse_url = pulse_url
        self.task_id = task_id
        self.project_id = project_id
        self.current_step = 0
        self.is_running = True

        # Resolve project_id and project_path from task if not provided
        self.project_path = None
        if self.task_id:
            task = get_task_by_id(self.task_id)
            if task:
                self.project_id = task.get("project_id") or self.project_id
                self.project_path = task.get("project_path")

        role_data = load_role(persona)
        if not role_data:
            role_data = {
                "name": persona,
                "system_prompt": f"You are a {persona} agent.",
                "capabilities": [],
            }
        
        self.system_prompt = build_system_prompt(role_data, goal, project_path=self.project_path)
        self.history: List[Dict[str, str]] = []

        # Log agent start
        import sys
        cli_args = {
            "goal": goal,
            "persona": persona,
            "max_iterations": max_iterations,
            "job_id": job_id,
            "pulse_url": pulse_url,
            "task_id": task_id,
            "project_id": self.project_id,
        }
        log_agent_start(
            project_id=self.project_id,
            job_id=job_id,
            persona=persona,
            system_prompt=self.system_prompt,
            args=cli_args,
            goal=goal,
            task_id=task_id,
        )

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
                print(f"[Runtime] Warning: Telemetry failed ({resp.status_code}) to {self.pulse_url}")
        except Exception as e:
            # Non-fatal: telemetry is best-effort. Surface it once (quietly)
            # instead of swallowing it — a silent failure is what hid the
            # 8080/8081 port mismatch for so long.
            if not getattr(self, "_telemetry_warned", False):
                print(f"[Runtime] Telemetry unavailable at {self.pulse_url}: {e}")
                self._telemetry_warned = True

    def run(self):
        """Execute the ReAct reasoning loop."""
        print(f"--- Agent [{self.job_id}] Starting ---")
        print(f"Goal: {self.goal}")
        print(f"Persona: {self.persona}")
        print("System prompt")
        print(self.system_prompt)

        self.send_telemetry("initializing", f"Starting agent for goal: {self.goal[:50]}...")
        append_event(
            "system:runtime",
            {"action": "started", "job_id": self.job_id, "persona": self.persona, "goal": self.goal},
        )

        try:
            for turn in range(self.max_iterations):
                self.current_step += 1
                print(f"\n--- Turn {self.current_step}/{self.max_iterations} ---")
                #print(self.history[-1]["content"] if self.history else "[No history yet]")

                # --- Think ---
                # --- Think ---
                state = "thinking"
                self.send_telemetry(state, f"Reasoning step {self.current_step}")
                print(f"[Thinking] Analyzing current state...")
                log_agent_step(
                    project_id=self.project_id,
                    job_id=self.job_id,
                    turn=self.current_step,
                    phase="thinking",
                )

                # Build message history for the LLM
                messages = [{"role": "system", "content": self.system_prompt}]
                if not self.history:
                    # First turn: add and save the initial user prompt to preserve role alternation
                    initial_user_msg = {"role": "user", "content": f"Please begin the task. Goal: {self.goal}"}
                    messages.append(initial_user_msg)
                    self.history.append(initial_user_msg)
                else:
                    for msg in self.history:
                        messages.append(msg)

                response = query_llm(
                    messages=messages,
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
                    log_agent_step(
                        project_id=self.project_id,
                        job_id=self.job_id,
                        turn=self.current_step,
                        phase="final_answer",
                        detail=final_result[:1000],
                    )
                    log_agent_complete(
                        project_id=self.project_id,
                        job_id=self.job_id,
                        success=True,
                        result=final_result[:1000],
                    )
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

                    log_agent_step(
                        project_id=self.project_id,
                        job_id=self.job_id,
                        turn=self.current_step,
                        phase="action",
                        tool_name=tool_name,
                        tool_params=params,
                    )

                    # Prepare context with project path and role-based constraints
                    exec_context = {"project_path": self.project_path}
                    role_data = load_role(self.persona)
                    if role_data and "operational_constraints" in role_data:
                        exec_context["allowed_tools"] = role_data["operational_constraints"].get("allowed_tools", [])

                    # Log opencode calls specifically
                    if tool_name in ("opencode_task", "opencode_run"):
                        import time as _time
                        _start = _time.time()
                        observation = execute_tool(tool_name, params, context=exec_context)
                        _duration = (_time.time() - _start) * 1000
                        log_opencode_task(
                            project_id=self.project_id,
                            job_id=self.job_id,
                            task_description=params.get("task_description", ""),
                            system_prompt=params.get("system_prompt", ""),
                            params=params,
                            response=observation,
                            duration_ms=_duration,
                        )
                    else:
                        # For all other tools (shell, file, etc.), inject the project path and constraints
                        observation = execute_tool(tool_name, params, context=exec_context)

                    obs_str = json.dumps(observation)
                    print(f"[Observation]: {obs_str[:300]}...")


                    log_agent_step(
                        project_id=self.project_id,
                        job_id=self.job_id,
                        turn=self.current_step,
                        phase="observation",
                        observation=obs_str[:2000],
                    )

                    self.history.append(
                        {"role": "user", "content": f"OBSERVATION: {obs_str}"}
                    )
                else:
                    # No action or final answer; nudge the agent
                    print("[!] No ACTION or FINAL ANSWER detected. Nudging agent.")
                    nudge = "Please use the proper format: either provide an ACTION/ARGS block or a FINAL ANSWER."
                    log_agent_step(
                        project_id=self.project_id,
                        job_id=self.job_id,
                        turn=self.current_step,
                        phase="nudge",
                        detail=nudge,
                    )
                    self.history.append({"role": "user", "content": nudge})

            # Max iterations reached without completion
            error_msg = "Exceeded maximum reasoning turns. Agent could not complete the goal."
            log_agent_complete(
                project_id=self.project_id,
                job_id=self.job_id,
                success=False,
                error=error_msg,
            )
            raise Exception(error_msg)

        except KeyboardInterrupt:
            print("\n[Runtime] Interrupted by user.")
            log_agent_complete(
                project_id=self.project_id,
                job_id=self.job_id,
                success=False,
                error="Interrupted by user.",
            )
            self.send_telemetry("interrupted", "Process killed.")
            append_event(
                "system:runtime",
                {"action": "interrupted", "job_id": self.job_id},
            )
            self._finalize_task("Interrupted by user.", success=False)
        except Exception as e:
            error_msg = str(e)
            print(f"\n[Runtime] Error: {error_msg}")
            log_agent_complete(
                project_id=self.project_id,
                job_id=self.job_id,
                success=False,
                error=error_msg,
            )
            self.send_telemetry("failed", error_msg)
            append_event(
                "system:runtime",
                {"action": "failed", "job_id": self.job_id, "error": error_msg},
            )
            self._finalize_task(error_msg, success=False)

    def _finalize_task(self, result: str, success: bool):
        """Update task status and project state upon completion.

        A successful run does NOT self-certify "done" — it goes through the
        stage-gate pipeline like every other execution path. If the gates
        cannot run, the task is left in pending_verification (fail closed),
        never silently completed.
        """
        if self.task_id:
            if success:
                try:
                    from tools.stage_gate_tool import run_full_pipeline
                    pipeline = run_full_pipeline(
                        self.task_id, developer_notes=f"Runtime result: {result[:800]}"
                    )
                    append_event(
                        "system:runtime",
                        {
                            "action": "stage_gates",
                            "job_id": self.job_id,
                            "task_id": self.task_id,
                            "passed": pipeline.get("success", False),
                            "stopped_at": pipeline.get("stopped_at"),
                        },
                    )
                except Exception as e:
                    try:
                        from registry_tool import update_task_status
                        update_task_status(
                            self.task_id,
                            "pending_verification",
                            notes=f"Stage gates unavailable ({e}); awaiting verification. "
                                  f"Runtime result: {result[:400]}",
                        )
                    except ImportError:
                        pass
            else:
                try:
                    from registry_tool import update_task_status
                    update_task_status(
                        self.task_id, "failed", notes=f"Runtime result: {result[:500]}"
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
    parser.add_argument(
        "--pulse_url",
        default=os.environ.get("PULSE_SERVER_URL", "http://127.0.0.1:8080"),
        help="Pulse Server URL",
    )
    parser.add_argument("--task_id", type=int, default=None, help="Registry task ID")
    parser.add_argument("--project_id", default=None, help="Project ID for .sdlc/ logging")

    args = parser.parse_args()

    runtime = AgentRuntime(
        goal=args.goal,
        persona=args.persona,
        max_iterations=args.max_iterations,
        job_id=args.job_id,
        pulse_url=args.pulse_url,
        task_id=args.task_id,
        project_id=args.project_id,
    )
    runtime.run()
