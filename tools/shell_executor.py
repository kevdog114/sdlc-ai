"""Shell executor for agent tool use.

Hardening: every command runs with a timeout (a hung command can no longer
wedge the pipeline forever) and output is capped so a runaway process can't
flood the state store or the LLM context. Commands still run through the
shell (agents emit shell syntax); real isolation — containers/network policy
— is a deployment concern documented in docs/REVIEW.md P1-1 and should gate
any multi-tenant or non-local use.
"""

import os
import subprocess

from bootstrap import append_event

DEFAULT_TIMEOUT = int(os.environ.get("SDLCAI_SHELL_TIMEOUT", "120"))
MAX_OUTPUT_CHARS = 20_000


def execute_command(command: str, timeout: int = DEFAULT_TIMEOUT, cwd: str = None) -> dict:
    """Executes a shell command with a timeout and logs the event."""
    print(f"[shell_executor] Executing: {command[:200]}")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = {
            "exit_code": result.returncode,
            "stdout": result.stdout.strip()[-MAX_OUTPUT_CHARS:],
            "stderr": result.stderr.strip()[-MAX_OUTPUT_CHARS:],
        }
    except subprocess.TimeoutExpired:
        output = {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
        }
    except Exception as e:
        output = {
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
        }

    append_event("tool:shell_executor", {
        "command": command[:500],
        "exit_code": output["exit_code"],
        "success": output["exit_code"] == 0,
    })

    return output


if __name__ == "__main__":
    # Simple test
    print(execute_command('echo "Hello from shell executor"'))
