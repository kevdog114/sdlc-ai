"""File manager for agent tool use — confined to project directories.

Agents previously had unrestricted read/write/delete on any path the process
could reach (~/.ssh, /etc, ...). Every operation now resolves its path and
requires it to sit under an allowed root:

  - the system root (bootstrap.BASE_DIR)
  - the generated-projects root (project_tool.USER_PROJECTS_ROOT)
  - extra roots from SDLCAI_FILE_ROOTS (os.pathsep-separated), for operators
    who deliberately point agents somewhere else

Symlink and ".." escapes are neutralized by resolving before the check.
"""

import os
from pathlib import Path
from typing import List

import bootstrap
from bootstrap import append_event


def _allowed_roots() -> List[Path]:
    roots = [Path(bootstrap.BASE_DIR).resolve()]
    try:
        from tools import project_tool
        roots.append(Path(project_tool.USER_PROJECTS_ROOT).resolve())
    except Exception:
        pass
    for extra in os.environ.get("SDLCAI_FILE_ROOTS", "").split(os.pathsep):
        if extra.strip():
            roots.append(Path(extra.strip()).resolve())
    return roots


def _confine(path: str) -> Path:
    """Resolve a path and require it to be under an allowed root."""
    resolved = Path(path).resolve()
    for root in _allowed_roots():
        if resolved == root or root in resolved.parents:
            return resolved
    raise PermissionError(
        f"Access denied: {path} is outside the allowed project directories"
    )


def read_file(path: str) -> dict:
    """Reads the content of a file (within the allowed roots)."""
    try:
        p = _confine(path)
        if not p.is_file():
            raise FileNotFoundError(f"Not a file: {path}")
        content = p.read_text(encoding="utf-8")
        append_event("tool:file_manager", {"action": "read", "path": path, "success": True})
        return {"success": True, "content": content}
    except Exception as e:
        append_event("tool:file_manager", {"action": "read", "path": path, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}


def write_file(path: str, content: str) -> dict:
    """Writes (or overwrites) a file (within the allowed roots)."""
    try:
        p = _confine(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        append_event("tool:file_manager", {"action": "write", "path": path, "success": True})
        return {"success": True}
    except Exception as e:
        append_event("tool:file_manager", {"action": "write", "path": path, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}


def list_dir(path: str) -> dict:
    """Lists the contents of a directory (within the allowed roots)."""
    try:
        p = _confine(path)
        if not p.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        files = [f.name for f in p.iterdir()]
        append_event("tool:file_manager", {"action": "list", "path": path, "success": True})
        return {"success": True, "files": files}
    except Exception as e:
        append_event("tool:file_manager", {"action": "list", "path": path, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}


def delete_file(path: str) -> dict:
    """Deletes a file (within the allowed roots)."""
    try:
        p = _confine(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
        p.unlink()
        append_event("tool:file_manager", {"action": "delete", "path": path, "success": True})
        return {"success": True}
    except Exception as e:
        append_event("tool:file_manager", {"action": "delete", "path": path, "success": False, "error": str(e)})
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    # Simple Test (paths under the project root)
    print("[test] Writing test.txt...")
    print(write_file(str(Path(bootstrap.BASE_DIR) / "test.txt"), "Hello from File Manager!"))
    print("[test] Reading test.txt...")
    print(read_file(str(Path(bootstrap.BASE_DIR) / "test.txt")))
    print("[test] Deleting test.txt...")
    print(delete_file(str(Path(bootstrap.BASE_DIR) / "test.txt")))
