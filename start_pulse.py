import sys
import time
import os

# Add project root to path
project_root = "/Users/klschaefer/dev-projects/sdlc-ai"
if project_root not in sys.path:
    sys.path.insert(0, project_root)

print(f"Starting pulse_server from {project_root}...")

try:
    from tools.pulse_server import start_pulse_server
    url = start_pulse_server(host='0.0.0.0', port=8080)
    print(f"Server is running at {url}")
except Exception as e:
    print(f"Failed to start server: {e}")
    sys.exit(1)

# Keep the process alive if start_pulse_server returns instead of blocking
while True:
    time.sleep(3600)
