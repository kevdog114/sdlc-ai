import os
import sys
import time
from pathlib import Path

# Add project root to path (portable: derived from this file's location)
project_root = str(Path(__file__).resolve().parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

print(f"Starting pulse_server from {project_root}...")

try:
    from tools.pulse_server import start_pulse_server
    # Loopback by default; set SDLCAI_HOST (plus SDLCAI_API_TOKEN) to expose.
    host = os.environ.get("SDLCAI_HOST", "127.0.0.1")
    url = start_pulse_server(host=host, port=8080)
    print(f"Server is running at {url}")
except Exception as e:
    print(f"Failed to start server: {e}")
    sys.exit(1)

# Keep the process alive if start_pulse_server returns instead of blocking
while True:
    time.sleep(3600)
