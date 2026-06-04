import subprocess
import json
from datetime import datetime, timezone
from pathlib import Path
from bootstrap import append_event

def read_file(path: str) -> dict:
    """Reads the content of a file."""
    p = Path(path)
    try:
        if not p.is_file():
            raise FileNotFoundError(f"Not a file: {path}")
        content = p.read_text(encoding="utf-8")
        append_event("tool:file_manager", {"action": "read", "path": path, "success": True})
        return {"success": True, "content": content}
    except Exception as e:
        append_event("tool:file_manager", {"action": "read", "path": path, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}

def write_file(path: str, content: str) -> dict:
    """Writes (or overwrites) a file."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        append_event("tool:file_manager", {"action": "write", "path": path, "success": True})
        return {"success": True}
    except Exception as e:
        append_event("tool:file_manager", {"action": "write", "path": path, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}

def list_dir(path: str) -> dict:
    """Lists the contents of a directory."""
    p = Path(path)
    try:
        if not p.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        files = [f.name for f in p.iterdir()]
        append_event("tool:file_manager", {"action": "list", "path": path, "success": True})
        return {"success": True, "files": files}
    except Exception as e:
        append_event("tool:file_manager", {"action": "list", "path": path, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}

def delete_file(path: str) -> dict:
    """Deletes a file."""
    p = Path(path)
    try:
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
        p.unlink()
        append_event("tool:file_manager", {"action": "delete", "path": path, "success": True})
        return {"success": True}
    except Exception as e:
        append_event("tool:file_manager", {"action": "delete", "path": path, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    # Simple Test
    print("[test] Writing test.txt...")
    print(write_file("test.txt", "Hello from File Manager!"))
    print("[test] Reading test.txt...")
    print(read_file("test.txt"))
    print("[test] Listing current dir...")
    print(list_dir("."))
    print("[test] Deleting test.txt...")
    print(delete_file("test.txt"))
