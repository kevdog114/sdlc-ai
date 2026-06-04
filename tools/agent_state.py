import json
import os
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

# Directory for agent-to-agent communication/state
STATE_DIR = Path("/tmp/sdlc-ai-state")
STATE_DIR.mkdir(parents=True, exist_ok=True)

def write_memo(sender: str, receiver: str, subject: str, content: str) -> dict:
    """
    Writes a 'memo' from one agent to another via the shared state registry.
    The receiver can then read this memo using `read_memos`.
    """
    memo_id = f"{sender}_to_{receiver}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    memo_path = STATE_DIR / f"{memo_id}.json"
    
    memo_data = {
        "memo_id": memo_id,
        "sender": sender,
        "receiver": receiver,
        "subject": subject,
        "content": content,
        "timestamp": datetime.now().isoformat()
    }
    
    try:
        with open(memo_path, "w") as f:
            json.dump(memo_data, f, indent=2)
        return {"success": True, "memo_id": memo_id}
    except Exception as e:
        return {"success": False, "error": str(e)}

def read_memos(receiver: str) -> dict:
    """
    Reads all pending memos addressed to a specific agent.
    Once read, the memos are considered 'delivered' and can be archived/deleted (for now we just list them).
    """
    try:
        memos = []
        for memo_file in STATE_DIR.glob("*.json"):
            with open(memo_file, "r") as f:
                data = json.load(f)
                if data["receiver"] == receiver:
                    memos.append(data)
        
        return {"success": True, "memos": memos}
    except Exception as e:
        return {"success": False, "error": str(e)}

def clear_memo(memo_id: str) -> dict:
    """Deletes a memo after it has been processed."""
    memo_path = STATE_DIR / f"{memo_id}.json"
    if memo_path.exists():
        try:
            memo_path.unlink()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    return {"success": False, "error": "Memo not found."}

if __name__ == "__main__":
    # Simple test logic
    print("[test] Writing memo...")
    res = write_memo("architect", "developer", "Task Update", "The system architecture is ready.")
    print(res)
    if res["success"]:
        import time
        time.sleep(1)
        print("[test] Reading memos for developer...")
        read_res = read_memos("developer")
        print(read_res)
        if read_res["success"] and read_res["memos"]:
            mid = read_res["memos"][0]["memo_id"]
            print(f"[test] Clearing memo {mid}...")
            clear_res = clear_memo(mid)
            print(clear_res)
