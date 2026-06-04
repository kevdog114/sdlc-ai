"""Pulse Server — FastAPI backend with WebSocket real-time updates."""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Set, Optional
import asyncio
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import bootstrap

# --- Path Discovery --------------------------------------------

def find_project_root() -> Path:
    """Finds the project root by looking for the 'state' directory."""
    current = Path(__file__).resolve().parent
    # If we are in tools/, go up one level. If we are in root, stay here.
    if current.name == "tools":
        current = current.parent
    
    # Check if this is the root by looking for state/
    for parent in [current] + list(current.parents):
        if (parent / "state").is_dir():
            return parent
    return current

PROJECT_ROOT = find_project_root()

# Override bootstrap paths to ensure they use the discovered PROJECT_ROOT
bootstrap.BASE_DIR = PROJECT_ROOT
bootstrap.STATE_DIR = PROJECT_ROOT / "state"
bootstrap.TASK_REGISTRY_PATH = PROJECT_ROOT / "state" / "task_registry.json"
bootstrap.STATE_FILE_PATH = PROJECT_ROOT / "state" / "project_state.json"
bootstrap.EVENT_LOG_PATH = PROJECT_ROOT / "logs" / "event_log.jsonl"

# Update local Pulse Server paths
HTML_PATH = PROJECT_ROOT / "tools" / "radar.html"

print(f"DEBUG: Detected Project Root: {PROJECT_ROOT}")
print(f"DEBUG: Using TASK_REGISTRY_PATH: {bootstrap.TASK_REGISTRY_PATH}")

app = FastAPI(title="Command Center Pulse")

# State management
connected_clients: List[WebSocket] = []
registry_position = 0
event_position = 0
lock = threading.Lock()

_cached_registry: Dict[str, Any] = {}
_cached_event_lines: List[str] = []
_active_agents: Dict[str, Any] = {} # agent_id -> latest_telemetry
_main_loop: Optional[asyncio.AbstractEventLoop] = None

def _safe_load_registry() -> Dict[str, Any]:
    try:
        if bootstrap.TASK_REGISTRY_PATH.exists():
            with open(bootstrap.TASK_REGISTRY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError, PermissionError):
        pass
    return {}

def _safe_read_events() -> List[str]:
    try:
        if bootstrap.EVENT_LOG_PATH.exists():
            content = bootstrap.EVENT_LOG_PATH.read_text(encoding="utf-8").strip()
            if content:
                return content.split("\n")
    except (OSError, PermissionError):
        pass
    return []

def _load_initial_state():
    global _cached_registry, _cached_event_lines, registry_position, event_position
    print("DEBUG: Running _load_initial_state...")
    _cached_registry = _safe_load_registry()
    _cached_event_lines = _safe_read_events()
    registry_position = len(_cached_registry.get("tasks", []))
    event_position = len(_cached_event_lines)
    print(f"DEBUG: Initial state loaded. Registry tasks: {len(_cached_registry.get('tasks', []))}, Events lines: {len(_cached_event_lines)}")

_load_initial_state()

def _broadcast(payload: Dict[str, Any]):
    global _main_loop
    if _main_loop is None:
        return

    disconnected: List[WebSocket] = []
    for ws in connected_clients:
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), _main_loop)
        except Exception:
            disconnected.append(ws)

    for ws in disconnected:
        if ws in connected_clients:
            connected_clients.remove(ws)

class _FileChangeHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.is_directory:
            return
        src = Path(event.src_path).resolve()
        if src == bootstrap.TASK_REGISTRY_PATH.resolve():
            self._handle_registry_change()
        elif src == bootstrap.EVENT_LOG_PATH.resolve():
            self._handle_event_change()

    def _handle_registry_change(self):
        global _cached_registry, registry_position
        time.sleep(0.1)  # debounce
        with lock:
            new_registry = _safe_load_registry()
            new_tasks = new_registry.get("tasks", [])
            _cached_registry = new_registry

            delta_tasks = new_tasks[registry_position:]
            if delta_tasks:
                payload = {
                    "type": "registry_update",
                    "tasks": delta_tasks,
                    "total_tasks": len(new_tasks),
                    "next_id": new_registry.get("next_id"),
                }
                _broadcast(payload)
                registry_position = len(new_tasks)

    def _handle_event_change(self):
        global _cached_event_lines, event_position
        time.sleep(0.1)  # debounce
        with lock:
            new_lines = _safe_read_events()
            delta_lines = new_lines[event_position:]
            _cached_event_lines = new_lines

            if delta_lines:
                events = []
                for line in delta_lines:
                    try:
                        events.append(json.loads(line.strip()))
                    except (json.JSONDecodeError, TypeError):
                        continue
                if events:
                    payload = {
                        "type": "event_update",
                        "events": events,
                    }
                    _broadcast(payload)
                    event_position = len(new_lines)

print("DEBUG: Setting up observer...")
observer = Observer()
handler = _FileChangeHandler()
observer.schedule(handler, str(PROJECT_ROOT), recursive=True)
observer.start()
print("DEBUG: Observer started.")

# --- API Routes ---

@app.get("/api/registry")
async def get_registry():
    return _safe_load_registry()

@app.get("/api/events")
async def get_events(limit: int = 100):
    lines = _safe_read_events()
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line.strip()))
        except (json.JSONDecodeError, TypeError):
            continue
    return {"events": events}

@app.post("/api/telemetry")
async def receive_telemetry(request: Request):
    """Receives telemetry from agents and broadcasts to clients."""
    global _active_agents
    try:
        payload = await request.json()
        agent_id = payload.get("agent_id")
        if not agent_id:
            return {"status": "error", "message": "Missing agent_id"}

        # Update local state for snapshotting/dashboarding
        with lock:
            _active_agents[agent_id] = {
                **payload,
                "last_seen": time.time()
            }

        # Broadcast to all connected WebSockets
        _broadcast({
            "type": "telemetry_update",
            "data": payload
        })
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/agents")
async def get_active_agents():
    """Returns currently tracked agents (merged from telemetry and project state)."""
    now = time.time()
    with lock:
        # 1. Cleanup stale in-memory telemetry data (older than 5 mins)
        to_remove = [aid for aid, data in _active_agents.items() if now - data["last_seen"] > 300]
        for aid in to_remove:
            del _active_agents[aid]

        # 2. Get agents from project state (the source of truth for spawned processes)
        state = bootstrap.load_project_state()
        disk_agents = state.get("active_agents", {})

        # 3. Merge them
        merged_agents = {}
        
        for agent_id, metadata in disk_agents.items():
            merged_agents[agent_id] = {
                "agent_id": agent_id,
                "persona": metadata.get("persona", "unknown"),
                "goal": metadata.get("goal", ""),
                "status": metadata.get("status", "running"),
                "pid": metadata.get("pid"),
                "last_seen": 0,
                "is_live": False
            }

        for agent_id, telemetry in _active_agents.items():
            if agent_id in merged_agents:
                merged_agents[agent_id].update({
                    **telemetry,
                    "is_live": True
                })
            else:
                merged_agents[agent_id] = {
                    **telemetry,
                    "is_live": True
                }

        return list(merged_agents.values())

# --- Core Routes ---

@app.get("/")
async def index():
    if HTML_PATH.exists():
        return FileResponse(HTML_PATH)
    return {"error": "radar.html not found"}

@app.get("/health")
async def health():
    return {
        "status": "ok", 
        "clients": len(connected_clients), 
        "agents": len(_active_agents)
    }

@app.websocket("/radar-ws")
async def websocket_endpoint(websocket: WebSocket):
    global _main_loop
    import asyncio
    _main_loop = asyncio.get_running_loop()

    await websocket.accept()
    connected_clients.append(websocket)
    try:
        with lock:
            events = []
            for line in _cached_event_lines[-50:]:
                try:
                    events.append(json.loads(line))
                except: continue

            snapshot = {
                "type": "initial_state",
                "registry": _cached_registry,
                "events": events,
                "agents": list(_active_agents.values())
            }

        await websocket.send_json(snapshot)

        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WS Error: {e}")
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)

print("DEBUG: App mounted.")

def start_pulse_server(host: str = "127.0.0.1", port: int = 8080) -> str:
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", reload=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1) 
    return f"http://{host}:{port}"

def stop_pulse_server():
    import uvicorn
    for obj in globals().values():
        if isinstance(obj, uvicorn.Server):
            obj.should_exit = True
            break

print("DEBUG: pulse_server module loading complete.")

if __name__ == "__main__":
    import uvicorn
    import sys
    host = "127.0.0.1"
    port = 8080
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    uvicorn.run(app, host=host, port=port, log_level="info")
