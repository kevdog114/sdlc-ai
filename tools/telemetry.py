import json
import urllib.request
import urllib.error
from datetime import datetime
from typing import Any, Dict, Optional
import logging

logger = logging.getLogger("TelemetryClient")

class TelemetryClient:
    """
    A lightweight client for agents to emit telemetry events to the Pulse Server.
    Uses standard library `urllib` to ensure zero external dependencies.
    """
    def __init__(self, pulse_server_url: str = "http://localhost:8081", agent_id: str = "", persona: str = "unknown"):
        self.endpoint = f"{pulse_server_url.rstrip('/')}/telemetry"
        self.agent_id = agent_id
        self.persona = persona

    def emit(self, state: str, iteration: int = 0, tool_name: Optional[str] = None, 
             tool_arguments: Optional[Dict[str, Any]] = None, 
             observation: Optional[str] = None, 
             goal: Optional[str] = None,
             error: Optional[str] = None,
             exit_code: Optional[int] = None):
        """
        Constructs and sends a telemetry event.
        """
        payload = {
            "agent_id": self.agent_id,
            "persona": self.persona,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "state": state,
            "iteration": iteration,
        }

        # Add conditional fields based on state
        if goal: payload["goal"] = goal
        if tool_name: payload["tool_name"] = tool_name
        if tool_arguments: payload["tool_arguments"] = tool_arguments
        if observation: payload["observation"] = observation
        if error: payload["error"] = error
        if exit_code is not None: payload["exit_code"] = exit_code

        self._send(payload)

    def _send(self, payload: Dict[str, Any]):
        """Performs the actual HTTP POST."""
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                self.endpoint, 
                data=data, 
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status not in (200, 201, 204):
                    logger.warning(f"Telemetry server returned non-success status: {response.status}")
        except urllib.error.URLError as e:
            # We don't want telemetry failures to crash the agent!
            # Log it and move on.
            logger.debug(f"Could not send telemetry (Pulse Server might be down): {e}")
        except Exception as e:
            logger.error(f"Unexpected error in TelemetryClient: {e}")

if __name__ == "__main__":
    # Test the client
    client = TelemetryClient(agent_id="test-agent-123", persona="tester")
    print("Sending test heartbeat...")
    client.emit(state="thinking", iteration=1, goal="Test mission", observation="Ready to go.")
    print("Done (check Pulse Server logs if running).")
