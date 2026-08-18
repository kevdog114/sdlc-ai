import json
import sys
import time
from datetime import datetime
from typing import Any, Dict, List

# Add project root to sys.path so we can import tools and bootstrap
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

try:
    from tools.project_tool import submit_project, resume_project, get_project_status, PHASE_BA_ANALYSIS, PROJECT_STATUS_AWAITING_CLARIFICATION, PROJECT_STATUS_COMPLETED, PROJECT_STATUS_FAILED
    import bootstrap
except ImportError as e:
    print(f"Error: Could not import project tools. Ensure this script is in the project root. {e}")
    sys.exit(1)

# ANSI Color codes for pretty terminal output
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def log(level: str, message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    color = Colors.ENDC
    if level == "INFO": color = Colors.BLUE
    elif level == "SUCCESS": color = Colors.GREEN
    elif level == "WARN": color = Colors.YELLOW
    elif level == "ERROR": color = Colors.RED
    elif level == "PHASE": color = Colors.HEADER + Colors.BOLD
    
    print(f"{Colors.CYAN}[{timestamp}]{Colors.ENDC} {color}{level:<8}{Colors.ENDC} {message}")

def run_turbo_pipeline(description: str, name: str):
    log("INFO", f"🚀 Starting Turbo Pipeline for project: '{name}'")
    log("INFO", f"Description: {description}")
    print("-" * 60)

    # Phase 1: Initial Submission
    result = submit_project(description, name=name)
    if not result.get("success"):
        log("ERROR", f"Initial submission failed: {result.get('error')}")
        return

    project_id = result["project_id"]
    log("SUCCESS", f"Project created! ID: {project_id}")

    # Main Loop: Keep running until complete or failed
    max_attempts = 10
    attempt = 0

    while attempt < max_attempts:
        status_data = get_project_status(project_id)
        if not status_data:
            log("ERROR", "Could not retrieve project status.")
            break

        phase = status_data.get("phase")
        state = status_data.get("status")

        # 1. Check if Complete
        if state == PROJECT_STATUS_COMPLETED:
            log("PHASE", f"🏁 PIPELINE COMPLETE!")
            summary = status_data.get("summary", {})
            log("SUCCESS", f"Total Stories: {summary.get('stories', 'N/A')}")
            log("SUCCESS", f"Tasks Succeeded: {summary.get('succeeded', 'N/A')}/{summary.get('total_tasks', 'N/A')}")
            break

        # 2. Check if Failed
        if state == PROJECT_STATUS_FAILED:
            log("ERROR", "❌ Pipeline failed.")
            log("ERROR", f"Last Error: {status_data.get('error', 'Unknown error')}")
            break

        # 3. Handle Clarifications (The "Turbo" part)
        if state == PROJECT_STATUS_AWAITING_CLARIFICATION:
            pending = status_data.get("clarifications", {}).get("pending_requests", [])
            if pending:
                log("WARN", f"Found {len(pending)} ambiguities in {phase}. Auto-answering for Turbo Mode...")
                for req in pending:
                    log("INFO", f"  - Resolving: {req['question'][:50]}...")
                
                # In Turbo Mode, we tell the tool to automatically answer all pending requests.
                res = resume_project(project_id, auto_answer=True)
                if not res.get("success"):
                    log("ERROR", f"Resume failed: {res.get('error')}")
                    break
            else:
                log("INFO", "No pending clarifications found, attempting resume...")
                resume_project(project_id)

        # 4. General Progress Reporting
        elif phase != "unknown":
             log("PHASE", f"Current Phase: {phase.upper()}")
        
        else:
            log("INFO", "Waiting for pipeline progress...")

        attempt += 1
        time.sleep(2) # Avoid spamming the server/console

    if attempt == max_attempts:
        log("WARN", "Reached max attempts. Pipeline may be stuck or taking a long time.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pipeline_test.py \"Project Description\" \"Project Name\"")
        sys.exit(1)

    desc = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "TurboProject"
    
    run_turbo_pipeline(desc, name)
