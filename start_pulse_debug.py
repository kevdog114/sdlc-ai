import sys
import time
import os
import subprocess

# Add project root to path
project_root = "/Users/klschaefer/dev-projects/sdlc-ai"
if project_root not in sys.path:
    sys.path.insert(0, project_root)

print(f"Starting Pulse Server with INFO logging...")

# Run uvicorn directly to get better control over logging and avoid thread issues during startup
cmd = [
    "/Users/klschaefer/dev-projects/sdlc-ai/.venv/bin/python", 
    "-m", "uvicorn", 
    "tools.pulse_server:app", 
    "--host", "0.0.0.0", 
    "--port", "8080", 
    "--log-level", "info"
]

process = subprocess.Popen(cmd)
print(f"Process started with PID {process.pid}")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Stopping server...")
    process.terminate()
    process.wait()
