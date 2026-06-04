import subprocess
import json
from datetime import datetime, timezone
from pathlib import Path
from bootstrap import append_event

def execute_command(command: str) -> dict:
    """Executes a shell command and logs the event."""
    print(f'[shell_executor] Executing: {command}')
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True
        )
        output = {
            'exit_code': result.returncode,
            'stdout': result.stdout.strip(),
            'stderr': result.stderr.strip()
        }
    except Exception as e:
        output = {
            'exit_code': -1,
            'stdout': '',
            'stderr': str(e)
        }

    append_event('tool:shell_executor', {
        'command': command,
        'exit_code': output['exit_code'],
        'success': output['exit_code'] == 0
    })

    return output

if __name__ == '__main__':
    # Simple test
    print(execute_command('echo "Hello from shell executor"'))
