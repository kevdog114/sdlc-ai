import argparse
import time
import sys
import json
import requests
from datetime import datetime
from pathlib import Path

class AgentRuntime:
    def __init__(self, goal, persona, max_iterations, job_id, pulse_url):
        self.goal = goal
        self.persona = persona
        self.max_iterations = max_iterations
        self.job_id = job_id
        self.pulse_url = pulse_url
        self.current_step = 0
        self.is_running = True

    def send_telemetry(self, state: str, detail: str = ""):
        """Sends a heartbeat to the Pulse Server."""
        payload = {
            "agent_id": self.job_id,
            "persona": self.persona,
            "state": state,
            "goal": self.goal,
            "detail": detail,
            "timestamp": datetime.now().isoformat()
        }
        try:
            # Using a timeout to ensure the agent doesn't hang if Pulse is down
            response = requests.post(f"{self.pulse_url}/api/telemetry", json=payload, timeout=2)
            if response.status_code != 200:
                print(f"[Runtime] Warning: Telemetry failed ({response.status_code})")
        except Exception as e:
            print(f"[Runtime] Warning: Could not connect to Pulse Server: {e}")

    def run(self):
        print(f"--- Agent [{self.job_id}] Starting ---")
        print(f"Goal: {self.goal}")
        print(f"Persona: {self.persona}")
        
        self.send_telemetry("initializing", f"Starting agent for goal: {self.goal[:50]}...")

        try:
            while self.current_step < self.max_iterations and self.is_running:
                self.current_step += 1
                print(f"\n[Step {self.current_step}/{self.max_iterations}]")
                
                # --- Simulation of Reasoning/Action Cycle ---
                # In a production implementation, this is where the LLM loop lives.
                
                state = "thinking"
                self.send_telemetry(state, f"Processing step {self.current_step}")
                time.sleep(2) # Simulate reasoning time

                print(f"[Thinking] Analyzing requirements for goal...")
                
                state = "acting"
                self.send_telemetry(state, f"Executing action in step {self.current_step}")
                print(f"[Acting] Performing sub-task: simulating tool call...")
                time.sleep(3) # Simulate action time (e.g., shell command)

                # Simulation of a successful "final response" at the end
                if self.current_step == self.max_iterations or self.current_step % 3 == 0:
                    state = "completing"
                    self.send_telemetry(state, "Approaching goal completion.")
                    break

            # Finalize
            print("\n--- Goal Achieved ---")
            print("FINAL RESPONSE: Task completed successfully according to the persona requirements.")
            self.send_telemetry("completed", "Task finished successfully.")

        except KeyboardInterrupt:
            print("\n[Runtime] Interrupted by user/system.")
            self.send_telemetry("interrupted", "Process killed.")
        except Exception as e:
            print(f"\n[Runtime] Error: {e}")
            self.send_telemetry("failed", str(e))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--persona", default="architect")
    parser.add_argument("--max_iterations", type=int, default=5)
    parser.add_argument("--job_id", required=True)
    parser.add_argument("--pulse_url", default="http://localhost:8081")

    args = parser.parse_args()

    runtime = AgentRuntime(
        goal=args.goal,
        persona=args.persona,
        max_iterations=args.max_iterations,
        job_id=args.job_id,
        pulse_url=args.pulse_url
    )
    runtime.run()
