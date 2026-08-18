"""Agent Worker — A ReAct-based autonomous process that executes tasks using tools."""

import json
import sys
import argparse
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add tools directory to path so we can import local modules
TOOLS_DIR = Path(__file__).parent.absolute()
if str(TOOLS_DIR) not in sys.path:
    sys.path.append(str(TOOLS_DIR))

try:
    from registry_tool import update_task_status, get_task_by_id
    from llm_tool import query_llm
    from bootstrap import append_event
    from agent_logger import log_agent_start, log_agent_step, log_agent_complete
    # Import tools for execution
    import shell_executor
    import file_manager
    # knowledge_tool might not be fully implemented or have different signatures; 
    # we'll add it defensively.
    try:
        import knowledge_tool
    except ImportError:
        knowledge_tool = None
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)

# --- Tool Registry for the Worker ---
# This mapping tells the LLM what tools it has and how to call them.
AVAILABLE_TOOLS = {
    "shell": {
        "func": shell_executor.execute_command,
        "desc": "Execute a shell command. Args: {'command': 'string'}"
    },
    "file_read": {
        "func": file_manager.read_file,
        "desc": "Read the content of a file. Args: {'path': 'string'}"
    },
    "file_write": {
        "func": file_manager.write_file,
        "desc": "Write content to a file (overwrites). Args: {'path': 'string', 'content': 'string'}"
    },
    "dir_list": {
        "func": file_manager.list_dir,
        "desc": "List files in a directory. Args: {'path': 'string'}"
    }
}

if knowledge_tool:
    AVAILABLE_TOOLS["knowledge_store"] = {
        "func": knowledge_tool.store_insight,
        "desc": "Store insight in long-term memory. Args: {'topic': 'string', 'content': 'string'}"
    }

def build_system_prompt(persona: str, description: str) -> str:
    """Constructs the ReAct system prompt."""
    tool_descriptions = "\n".join([f"- {name}: {info['desc']}" for name, info in AVAILABLE_TOOLS.items()])
    
    return f"""You are a professional {persona}. 

YOUR GOAL: {description}

RESOURCES & TOOLS:
You have access to the following tools. Use them to gather information or perform actions.
{tool_descriptions}

OPERATING PROTOCOL (ReAct):
To complete your task, you must think step-by-step. For every turn, follow this exact format:

THOUGHT: [Your reasoning about what is happening and what the next logical step should be.]
ACTION: {list(AVAILABLE_TOOLS.keys())}
ARGS: {{"name": "tool_name", "params": {{"arg_name": "value"}}}}
[Wait for Observation]

OBSERVATION: [The result returned by the tool.]

Repeat this cycle until you have enough information to conclude. When you are finished and have a final answer, use this format:

FINAL ANSWER: [Your complete, polished, and high-quality response to the original task description.]

IMPORTANT RULES:
1. If an ACTION fails, use your THOUGHT to explain why and try a different approach or tool.
2. Only one ACTION per turn.
3. Be thorough and verify your work using tools (e.g., read a file after writing it).
4. Your FINAL ANSWER must be the complete solution, not just a summary of what you did."""

def execute_tool(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Executes an imported tool function with provided parameters."""
    if name not in AVAILABLE_TOOLS:
        return {"success": False, "error": f"Unknown tool: {name}"}
    
    print(f"[*] Executing Tool: {name}({params})")
    try:
        result = AVAILABLE_TOOLS[name]["func"](**params)
        # Ensure result is a dict for consistency in the conversation history
        if not isinstance(result, dict):
            result = {"success": True, "output": str(result)}
        return result
    except Exception as e:
        print(f"[!] Tool Execution Error ({name}): {e}")
        return {"success": False, "error": str(e)}

def execute_task(task_id: int, description: str, persona: str, project_id: Optional[str] = None):
    """The main ReAct execution loop."""
    append_event("system:worker", {"action": "started", "task_id": task_id, "persona": persona})
    print(f"[*] Worker #{task_id} ({persona}) started.")

    # Resolve project_id from task if not provided
    if not project_id:
        task_data = get_task_by_id(task_id)
        if task_data and task_data.get("project_id"):
            project_id = task_data["project_id"]

    # Start the conversation history for the ReAct loop
    history = []
    system_prompt = build_system_prompt(persona, description)

    # Log agent start
    job_id = f"worker-{task_id}"
    log_agent_start(
        project_id=project_id,
        job_id=job_id,
        persona=persona,
        system_prompt=system_prompt,
        args={"task_id": task_id, "description": description, "persona": persona, "project_id": project_id},
        goal=description,
        task_id=task_id,
    )
    
    try:
        update_task_status(task_id, "in_progress", notes=f"Agent ({persona}) is reasoning...")
        printf(f"[*] Task #{task_id} is now in progress. Beginning ReAct loop.")
        # Max turns to prevent infinite loops
        for turn in range(15):
            
            print(f"\n--- Turn {turn+1} ---")
            log_agent_step(
                project_id=project_id,
                job_id=job_id,
                turn=turn + 1,
                phase="thinking",
            )
            
            # 1. Query LLM with current context
            # We include the system prompt and all previous thoughts/actions/observations
            messages = [{"role": "system", "content": system_prompt}]
            for msg in history:
                messages.append(msg)

            response = query_llm(
                prompt="Continue your reasoning loop. If you have a final answer, provide it using the FINAL ANSWER: format.",
                system_prompt=system_prompt, # Passing again for safety/context
                model="local",
                # Using temperature 0 for more stable tool calling
                temperature=0.0 
            )

            if not response["success"]:
                raise Exception(f"LLM Query failed: {response.get('error', 'Unknown error')}")

            content = response["content"].strip()
            print(f"[LLM Response]:\n{content}\n")
            history.append({"role": "assistant", "content": content})

            # 2. Check if the LLM provided a Final Answer
            if "FINAL ANSWER:" in content:
                final_result = content.split("FINAL ANSWER:")[1].strip()
                print(f"[+] Task #{task_id} complete.")
                log_agent_step(
                    project_id=project_id,
                    job_id=job_id,
                    turn=turn + 1,
                    phase="final_answer",
                    detail=final_result[:1000],
                )
                log_agent_complete(
                    project_id=project_id,
                    job_id=job_id,
                    success=True,
                    result=final_result[:1000],
                )
                update_task_status(task_id, "done", notes=f"Completed by {persona}. Result snippet: {final_result[:150]}...")
                append_event("system:worker", {"action": "completed", "task_id": task_id, "success": True})
                return

            # 3. Check for an ACTION/ARGS pattern
            # We search for the presence of both 'ACTION:' and 'ARGS:' to be sure
            if "ACTION:" in content and "ARGS:" in content:
                try:
                    # Simple parsing logic
                    action_line = [l for l in content.split("\n") if "ACTION:" in l][0]
                    args_line = [l for l in content.split("\n") if "ARGS:" in l][0]

                    tool_name = action_line.replace("ACTION:", "").strip()
                    # Handle list format ['tool'] or just 'tool'
                    tool_name = tool_name.strip("[]'\" ")
                    
                    args_json_str = args_line.replace("ARGS:", "").strip()
                    params = json.loads(args_json_str)

                    # 4. Execute the Tool
                    log_agent_step(
                        project_id=project_id,
                        job_id=job_id,
                        turn=turn + 1,
                        phase="action",
                        tool_name=tool_name,
                        tool_params=params,
                    )

                    observation = execute_tool(tool_name, params)
                    print(f"[OBSERVATION]: {observation}")

                    log_agent_step(
                        project_id=project_id,
                        job_id=job_id,
                        turn=turn + 1,
                        phase="observation",
                        observation=json.dumps(observation)[:2000],
                    )
                    
                    # Append observation to history for next LLM turn
                    history.append({"role": "user", "content": f"OBSERVATION: {json.dumps(observation)}"})

                except Exception as e:
                    error_obs = {"success": False, "error": f"Parsing error or execution crash: {str(e)}"}
                    print(f"[!] Loop Error: {e}")
                    history.append({"role": "user", "content": f"OBSERVATION: {json.dumps(error_obs)}"})
            else:
                # If no ACTION is found and no FINAL ANSWER is found, the agent might be stuck in a thought loop.
                print("[!] Agent provided response without ACTION or FINAL ANSWER. Forcing next turn.")
                nudge = "Please use the proper format: either provide an ACTION/ARGS block or a FINAL ANSWER."
                log_agent_step(
                    project_id=project_id,
                    job_id=job_id,
                    turn=turn + 1,
                    phase="nudge",
                    detail=nudge,
                )
                history.append({"role": "user", "content": nudge})

        error_msg = "Exceeded maximum reasoning turns (15). The agent is stuck in a loop."
        log_agent_complete(
            project_id=project_id,
            job_id=job_id,
            success=False,
            error=error_msg,
        )
        raise Exception(error_msg)

    except Exception as e:
        error_msg = str(e)
        print(f"[!] Task #{task_id} failed: {error_msg}")
        log_agent_complete(
            project_id=project_id,
            job_id=job_id,
            success=False,
            error=error_msg,
        )
        update_task_status(task_id, "failed", notes=f"Error: {error_msg}")
        append_event("system:worker", {"action": "failed", "task_id": task_id, "error": error_msg, "success": False})
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Agent Worker (ReAct)")
    parser.add_argument("--task-id", type=int, required=True, help="The ID of the task from the registry")
    parser.add_argument("--description", type=str, required=True, help="Description of the task")
    parser.add_argument("--persona", type=str, default="unassigned", help="Specialist persona")
    parser.add_argument("--project-id", type=str, default=None, help="Project ID for .sdlc/ logging")

    args = parser.parse_args()
    execute_task(args.task_id, args.description, args.persona, args.project_id)
