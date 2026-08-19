"""Pulse Server — FastAPI backend with WebSocket real-time updates."""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Set, Optional
import asyncio
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import bootstrap
bootstrap.ensure_initialized()


def _run_pipeline_in_thread(func, *args, **kwargs):
    """Run a blocking pipeline function in a daemon thread.

    FastAPI's BackgroundTasks can starve the event loop when the
    task contains synchronous blocking calls (e.g. LLM requests).
    This wrapper ensures the pipeline runs in a proper OS thread.
    """
    def _wrapper():
        try:
            func(*args, **kwargs)
        except Exception as e:
            print(f"[Pipeline Thread] Error: {e}")
            import traceback
            traceback.print_exc()

    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()
    return t

from tools import stage_gate_tool
from tools import clarification_tool
from tools import project_tool
from tools import telegram_bot

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

# Only override bootstrap's paths when they are still at their import-time
# default (i.e. nobody has redirected them). A test harness or embedder that
# has already pointed bootstrap at another location must not be clobbered —
# the routes read bootstrap.* at call time, so respecting the redirection is
# both correct and what keeps the suite hermetic.
_bootstrap_default_base = Path(bootstrap.__file__).resolve().parent
if Path(bootstrap.BASE_DIR).resolve() == _bootstrap_default_base:
    bootstrap.BASE_DIR = PROJECT_ROOT
    bootstrap.STATE_DIR = PROJECT_ROOT / "state"
    bootstrap.TASK_REGISTRY_PATH = PROJECT_ROOT / "state" / "task_registry.json"
    bootstrap.STATE_FILE_PATH = PROJECT_ROOT / "state" / "project_state.json"
    bootstrap.EVENT_LOG_PATH = PROJECT_ROOT / "logs" / "event_log.jsonl"

# Local Pulse Server path (radar.html lives beside this module).
HTML_PATH = Path(__file__).resolve().parent / "radar.html"


app = FastAPI(title="Command Center Pulse")

# CORS: the SPA is served from this same origin, so no cross-origin access is
# needed by default. The old wildcard-with-credentials combo let any web page
# the operator visited drive every state-changing endpoint. Operators who
# really need cross-origin access can set SDLCAI_CORS_ORIGINS (comma list).
_cors_origins = [o.strip() for o in os.environ.get("SDLCAI_CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ── API authentication ──────────────────────────────────────────
# When an API token is configured (SDLCAI_API_TOKEN or secrets), every /api/*
# request and the WebSocket must present it. Without a token the server only
# ever binds to loopback (enforced in the entry points below), so the control
# plane is reachable solely by the local operator.

_PUBLIC_PATHS = {"/", "/health"}


def _configured_api_token():
    try:
        from tools.secrets_tool import get_api_token
        return get_api_token()
    except Exception:
        return os.environ.get("SDLCAI_API_TOKEN")


def _request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-token", "") or request.query_params.get("token", "")


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    token = _configured_api_token()
    if token and request.url.path not in _PUBLIC_PATHS:
        import hmac
        if not hmac.compare_digest(_request_token(request), token):
            from starlette.responses import JSONResponse
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


def _require_token_for_nonlocal(host: str) -> None:
    """Fail closed: binding beyond loopback demands an API token."""
    if host not in ("127.0.0.1", "localhost", "::1") and not _configured_api_token():
        raise RuntimeError(
            f"Refusing to bind to {host}: no API token configured. "
            "Set SDLCAI_API_TOKEN (or .secrets/secrets.json api_token) to "
            "expose the control plane beyond localhost."
        )

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

_EVENT_TAIL = 500  # cap events held in memory / served to the dashboard


def _safe_read_events() -> List[str]:
    """Return the tail of the JSONL event log as raw lines."""
    try:
        if bootstrap.EVENT_LOG_PATH.exists():
            with open(bootstrap.EVENT_LOG_PATH, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            return lines[-_EVENT_TAIL:]
    except (OSError, PermissionError):
        pass
    return []


def _load_initial_state():
    global _cached_registry, registry_position, event_position, _cached_event_lines
    _cached_registry = _safe_load_registry()
    _cached_event_lines = _safe_read_events()
    event_position = len(_cached_event_lines)
    registry_position = len(_cached_registry.get("tasks", []))

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
        elif src == bootstrap.STATE_FILE_PATH.resolve():
            self._handle_state_change()
        elif hasattr(bootstrap, 'STORY_REGISTRY_PATH') and src == bootstrap.STORY_REGISTRY_PATH.resolve():
            self._handle_story_change()
        elif src == (PROJECT_ROOT / "state" / "clarifications.json").resolve():
            self._handle_clarification_change()

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
            lines = _safe_read_events()
            # Broadcast only genuinely new lines. If the log was rotated
            # (fewer lines than our position), resync without replaying.
            if len(lines) < event_position:
                event_position = len(lines)
                _cached_event_lines = lines
                return
            new_lines = lines[event_position:]
            _cached_event_lines = lines
            event_position = len(lines)
        new_events = []
        for line in new_lines:
            try:
                new_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if new_events:
            _broadcast({"type": "event_update", "events": new_events})

    def _handle_state_change(self):
        time.sleep(0.1)
        with lock:
            try:
                new_state = bootstrap.load_project_state()
                _broadcast({
                    "type": "state_update",
                    "kanban": new_state.get("kanban", {}),
                    "stories_kanban": new_state.get("stories_kanban", {}),
                    "active_agents": new_state.get("active_agents", {}),
                })
            except Exception:
                pass

    def _handle_story_change(self):
        time.sleep(0.1)
        with lock:
            try:
                import sys as _sys
                sys_path = str(PROJECT_ROOT)
                tools_path = str(PROJECT_ROOT / "tools")
                if sys_path not in _sys.path:
                    _sys.path.insert(0, sys_path)
                if tools_path not in _sys.path:
                    _sys.path.insert(0, tools_path)
                from story_tool import get_story_board_state
                board = get_story_board_state()
                _broadcast({
                    "type": "story_update",
                    "board": board,
                })
            except Exception:
                pass

    def _handle_clarification_change(self):
        time.sleep(0.1)
        with lock:
            try:
                import sys as _sys
                tools_path = str(PROJECT_ROOT / "tools")
                if tools_path not in _sys.path:
                    _sys.path.insert(0, tools_path)
                from clarification_tool import list_requests
                pending = list_requests(status="pending")
                answered = list_requests(status="answered")
                _broadcast({
                    "type": "clarification_update",
                    "pending": pending,
                    "answered": answered,
                })
            except Exception:
                pass

observer = Observer()
handler = _FileChangeHandler()
observer.schedule(handler, str(PROJECT_ROOT), recursive=True)
observer.start()

# --- API Routes ---

@app.get("/api/registry")
async def get_registry():
    return _safe_load_registry()
@app.get("/api/events")
async def get_events(limit: int = 100):
    """Returns the last N events from the JSONL event log."""
    limit = max(1, min(limit, _EVENT_TAIL))
    events = []
    for line in _safe_read_events()[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"events": events, "count": len(events)}

_TELEMETRY_MAX_AGENTS = 500


@app.post("/api/telemetry")
async def receive_telemetry(request: Request):
    """Receives telemetry from agents and broadcasts to clients."""
    global _active_agents
    try:
        payload = await request.json()
        agent_id = payload.get("agent_id")
        if not agent_id:
            return {"status": "error", "message": "Missing agent_id"}

        # Update local state for snapshotting/dashboarding. Bounded: a flood
        # of unique agent_ids must not grow memory without limit — evict the
        # stalest entry when the cap is hit.
        with lock:
            if agent_id not in _active_agents and len(_active_agents) >= _TELEMETRY_MAX_AGENTS:
                stalest = min(_active_agents, key=lambda a: _active_agents[a].get("last_seen", 0))
                del _active_agents[stalest]
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

@app.get("/api/kanban")
async def get_kanban():
    """Returns the kanban board state with task details per column."""
    try:
        sys_path = str(PROJECT_ROOT)
        import sys as _sys
        if sys_path not in _sys.path:
            _sys.path.insert(0, sys_path)
        if str(PROJECT_ROOT / "tools") not in _sys.path:
            _sys.path.insert(0, str(PROJECT_ROOT / "tools"))
        from kanban_tool import get_board_state, sync_all_tasks
        sync_all_tasks()
        return get_board_state()
    except ImportError:
        state = bootstrap.load_project_state()
        return state.get("kanban", {})

@app.get("/api/stories")
async def get_stories():
    """Returns all stories with derived status and progress."""
    try:
        sys_path = str(PROJECT_ROOT)
        import sys as _sys
        if sys_path not in _sys.path:
            _sys.path.insert(0, sys_path)
        if str(PROJECT_ROOT / "tools") not in _sys.path:
            _sys.path.insert(0, str(PROJECT_ROOT / "tools"))
        from story_tool import list_stories, sync_all_stories
        sync_all_stories()
        return list_stories()
    except ImportError:
        return []

@app.get("/api/story-kanban")
async def get_story_kanban():
    """Returns the story kanban board state."""
    try:
        sys_path = str(PROJECT_ROOT)
        import sys as _sys
        if sys_path not in _sys.path:
            _sys.path.insert(0, sys_path)
        if str(PROJECT_ROOT / "tools") not in _sys.path:
            _sys.path.insert(0, str(PROJECT_ROOT / "tools"))
        from story_tool import get_story_board_state
        return get_story_board_state()
    except ImportError:
        state = bootstrap.load_project_state()
        return state.get("stories_kanban", {})

@app.post("/api/stage-gate/submit")
async def stage_gate_submit(request: Request):
    """Submit a task for verification (developer done → QA)."""
    try:
        body = await request.json()
        task_id = body.get("task_id")
        notes = body.get("notes", "")
        if not task_id:
            return {"success": False, "error": "Missing task_id"}
        result = stage_gate_tool.submit_for_verification(task_id, notes)
        _broadcast({"type": "stage_gate_update", "data": result})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/stage-gate/qa")
async def stage_gate_qa(request: Request):
    """Run the QA verification gate."""
    try:
        body = await request.json()
        task_id = body.get("task_id")
        if not task_id:
            return {"success": False, "error": "Missing task_id"}
        result = stage_gate_tool.run_qa_gate(task_id)
        _broadcast({"type": "stage_gate_update", "data": result})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/stage-gate/architect")
async def stage_gate_architect(request: Request):
    """Run the architectural review gate."""
    try:
        body = await request.json()
        task_id = body.get("task_id")
        if not task_id:
            return {"success": False, "error": "Missing task_id"}
        result = stage_gate_tool.run_architect_gate(task_id)
        _broadcast({"type": "stage_gate_update", "data": result})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/stage-gate/full")
async def stage_gate_full(request: Request):
    """Run the complete stage-gate pipeline for a task."""
    try:
        body = await request.json()
        task_id = body.get("task_id")
        notes = body.get("notes", "")
        if not task_id:
            return {"success": False, "error": "Missing task_id"}
        result = stage_gate_tool.run_full_pipeline(task_id, notes)
        _broadcast({"type": "stage_gate_update", "data": result})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/stage-gate/status/{task_id}")
async def stage_gate_status(task_id: int):
    """Get the current pipeline status for a task."""
    try:
        return stage_gate_tool.get_pipeline_status(task_id)
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/state")
async def get_state():
    """Returns the project state with credentials masked.

    project_config is the documented storage location for e.g. the Telegram
    bot token — the raw state must never leave the process unredacted.
    """
    state = bootstrap.load_project_state()
    try:
        from tools.secrets_tool import redact
        return redact(state)
    except Exception:
        state.pop("project_config", None)
        return state

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

# ── Clarification Routes ────────────────────────────────────────

@app.get("/api/clarifications")
async def get_clarifications(project_id: str = None, status: str = None):
    """List clarification requests with optional filters."""
    try:
        import sys as _sys
        tools_path = str(PROJECT_ROOT / "tools")
        if tools_path not in _sys.path:
            _sys.path.insert(0, tools_path)
        from clarification_tool import list_requests
        return list_requests(project_id=project_id, status=status)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/clarifications/answer")
async def answer_clarification(request: Request):
    """Submit an answer to a clarification request."""
    try:
        body = await request.json()
        request_id = body.get("request_id")
        answer = body.get("answer")
        if not request_id or not answer:
            return {"success": False, "error": "Missing request_id or answer"}
        from clarification_tool import answer_request
        result = answer_request(request_id, answer, answered_by="dashboard")
        if result:
            _broadcast({"type": "clarification_answered", "request_id": request_id, "answer": answer[:200]})
            # Run resume in a daemon thread to avoid blocking the event loop
            def _try_resume():
                try:
                    from project_tool import resume_project as rp, _load_project
                    proj = _load_project(result.get("project_id", ""))
                    if proj and proj.get("status") == "awaiting_clarification":
                        resume_result = rp(result["project_id"])
                        if resume_result.get("status") == "completed":
                            _broadcast({"type": "project_update", "data": resume_result})
                except Exception:
                    pass
            _run_pipeline_in_thread(_try_resume)
            return {"success": True, "request": result, "message": "Answer recorded. Processing resumed in background."}
        return {"success": False, "error": "Request not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Project Routes ──────────────────────────────────────────────

@app.options("/api/projects/submit")
async def options_submit():
    return {"status": "ok"}

@app.post("/api/projects/submit")
async def submit_project(request: Request):
    """Submit a high-level project for the definition pipeline."""
    try:
        body = await request.json()
        description = body.get("description", "")
        name = body.get("name", "Untitled Project")
        if not description:
            return {"success": False, "error": "Missing project description"}

        from project_tool import create_project_record, submit_project as sp, _save_project

        # 1. Create the record immediately so we have an ID to return
        project = create_project_record(description, name)
        project["auto_execute"] = bool(body.get("auto_execute", False))
        _save_project(project)
        project_id = project["id"]

        # 2. Run the heavy pipeline in a daemon thread
        _run_pipeline_in_thread(sp, description, project_id=project_id)

        # 3. Return immediate response
        result = {
            "success": True,
            "project_id": project_id,
            "status": project["status"],
            "message": "Project submitted and analysis started in the background."
        }

        # Broadcast that a new project is being tracked
        _broadcast({"type": "project_update", "data": result})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}



@app.post("/api/projects/resume")
async def resume_project(request: Request):
    """Resume a project that was paused for clarifications."""
    try:
        body = await request.json()
        project_id = body.get("project_id")
        if not project_id:
            return {"success": False, "error": "Missing project_id"}

        def _do_resume():
            try:
                from project_tool import resume_project as rp
                result = rp(project_id)
                _broadcast({"type": "project_update", "data": result})
            except Exception as e:
                _broadcast({"type": "project_update", "data": {"error": str(e)}})

        _run_pipeline_in_thread(_do_resume)
        _broadcast({"type": "project_update", "data": {"project_id": project_id, "status": "resuming"}})
        return {"success": True, "project_id": project_id, "message": "Project resume started in background."}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/projects")
async def list_projects():
    """List all projects."""
    try:
        import sys as _sys
        tools_path = str(PROJECT_ROOT / "tools")
        if tools_path not in _sys.path:
            _sys.path.insert(0, tools_path)
        from project_tool import list_projects as lp
        return lp()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    """Get project status."""
    try:
        import sys as _sys
        tools_path = str(PROJECT_ROOT / "tools")
        if tools_path not in _sys.path:
            _sys.path.insert(0, tools_path)
        from project_tool import get_project_status
        result = get_project_status(project_id)
        if result:
            return result
        return {"error": "Project not found"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/projects/{project_id}/command")
async def project_command(project_id: str, request: Request):
    """Send a modification/prompt to an existing project.

    A project that already has requirements + architecture gets a DELTA
    change request (new stories appended to its backlog); a project still in
    definition gets a fresh BA run. The old behavior — re-running the whole
    pipeline on the command text and overwriting the project — is gone.
    """
    try:
        body = await request.json()
        command = body.get("command", "")
        if not command:
            return {"success": False, "error": "Missing command"}

        project = project_tool._load_project(project_id)
        if not project:
            return {"success": False, "error": f"Project {project_id} not found"}

        is_defined = bool(project.get("refined_requirements")) and bool(project.get("architecture"))

        def run_change():
            try:
                if is_defined:
                    result = project_tool.submit_change_request(project_id, command)
                else:
                    result = project_tool.submit_project(command, project_id=project_id)
                _broadcast({"type": "project_update", "data": {"project_id": project_id, "status": result.get("status", "processing")}})
                _broadcast({"type": "registry_update", "data": {}})
            except Exception as e:
                _broadcast({"type": "project_update", "data": {"project_id": project_id, "error": str(e)}})

        _run_pipeline_in_thread(run_change)

        mode = "change_request_started" if is_defined else "ba_analysis_started"
        _broadcast({"type": "project_update", "data": {"project_id": project_id, "status": mode, "command": command}})
        return {"success": True, "project_id": project_id, "status": mode,
                "message": "Change request analysis started." if is_defined else "Command sent for BA analysis."}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Backlog & Sprint Routes ─────────────────────────────────────

@app.get("/api/projects/{project_id}/backlog")
async def project_backlog(project_id: str):
    """The project's open product backlog (not in a sprint, not accepted)."""
    try:
        from tools.story_tool import list_backlog, derive_story_status
        stories = list_backlog(project_id)
        return [
            {**s, "derived_status": derive_story_status(s["id"])}
            for s in stories
        ]
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/sprints/plan")
async def plan_sprint_route(request: Request):
    """Propose a sprint from a project's backlog (nothing executes yet)."""
    try:
        body = await request.json()
        project_id = body.get("project_id")
        if not project_id:
            return {"success": False, "error": "Missing project_id"}
        from tools.sprint_tool import plan_sprint
        result = plan_sprint(
            project_id,
            capacity_points=body.get("capacity_points"),
            goal=body.get("goal"),
        )
        if result.get("success"):
            _broadcast({"type": "sprint_update", "data": result["sprint"]})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/sprints/{sprint_id}/start")
async def start_sprint_route(sprint_id: str):
    """PO approval: activate the sprint and execute it in the background."""
    try:
        from tools.sprint_tool import get_sprint
        sprint = get_sprint(sprint_id)
        if not sprint:
            return {"success": False, "error": f"Sprint {sprint_id} not found"}
        if sprint["status"] != "planning":
            return {"success": False, "error": f"Sprint is '{sprint['status']}', expected planning"}

        def run_sprint():
            try:
                from tools.sprint_tool import start_sprint
                result = start_sprint(sprint_id)
                _broadcast({"type": "sprint_update", "data": result.get("sprint", {"id": sprint_id})})
                _broadcast({"type": "registry_update", "data": {}})
            except Exception as e:
                _broadcast({"type": "sprint_update", "data": {"id": sprint_id, "error": str(e)}})

        _run_pipeline_in_thread(run_sprint)
        _broadcast({"type": "sprint_update", "data": {"id": sprint_id, "status": "starting"}})
        return {"success": True, "sprint_id": sprint_id, "message": "Sprint started; executing in background."}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/sprints")
async def sprints_route(project_id: str = ""):
    try:
        from tools.sprint_tool import list_sprints
        return list_sprints(project_id or None)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sprints/{sprint_id}")
async def sprint_detail_route(sprint_id: str):
    """Sprint detail with the per-story review summary for PO acceptance."""
    try:
        from tools.sprint_tool import review_sprint
        return review_sprint(sprint_id)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/stories/{story_id}/accept")
async def accept_story_route(story_id: str, request: Request):
    """PO acceptance decision. Rejections return the story to the backlog."""
    try:
        body = await request.json()
        if "accepted" not in body:
            return {"success": False, "error": "Missing 'accepted' (true/false)"}
        from tools.sprint_tool import accept_story
        result = accept_story(story_id, bool(body["accepted"]), notes=body.get("notes", ""))
        if result.get("success"):
            _broadcast({"type": "story_update", "data": result["story"]})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/sprints/{sprint_id}/complete")
async def complete_sprint_route(sprint_id: str):
    """Close a reviewed sprint: velocity from accepted points + retrospective."""
    try:
        from tools.sprint_tool import complete_sprint
        result = complete_sprint(sprint_id)
        if result.get("success"):
            _broadcast({"type": "sprint_update", "data": result["sprint"]})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/projects/{project_id}/velocity")
async def velocity_route(project_id: str, window: int = 3):
    try:
        from tools.sprint_tool import get_velocity
        return get_velocity(project_id, window=window)
    except Exception as e:
        return {"error": str(e)}


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

    # Same auth as the HTTP API: when a token is configured, the socket must
    # present it (query param — browsers can't set WS headers).
    token = _configured_api_token()
    if token:
        import hmac
        supplied = websocket.query_params.get("token", "")
        if not hmac.compare_digest(supplied, token):
            await websocket.close(code=4401)
            return

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


def start_pulse_server(host: str = "127.0.0.1", port: int = 8080) -> str:
    import uvicorn

    _require_token_for_nonlocal(host)

    # Start Telegram bot poller (if configured)
    def _on_telegram_answer(request_id: str):
        """Callback when a clarification is answered via Telegram."""
        _broadcast({"type": "clarification_answered", "request_id": request_id})
        try:
            from project_tool import resume_project as rp, _load_project
            from clarification_tool import get_request
            req = get_request(request_id)
            if req and req.get("project_id"):
                proj = _load_project(req["project_id"])
                if proj and proj.get("status") == "awaiting_clarification":
                    def _do_resume():
                        try:
                            resume_result = rp(req["project_id"])
                            if resume_result.get("status") == "completed":
                                _broadcast({"type": "project_update", "data": resume_result})
                        except Exception:
                            pass
                    _run_pipeline_in_thread(_do_resume)
        except Exception:
            pass

    telegram_bot.start_poller(on_answer=_on_telegram_answer)

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


def _cli():
    """Console-script entry point: run the pulse server (blocking).

    Loopback by default; exposing beyond localhost requires an explicit host
    (SDLCAI_HOST) AND a configured API token (fail closed).
    """
    import uvicorn
    import sys
    host = os.environ.get("SDLCAI_HOST", "127.0.0.1")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("SDLCAI_PORT", "8080"))
    _require_token_for_nonlocal(host)
    print(f"SDLC-AI pulse server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    _cli()
