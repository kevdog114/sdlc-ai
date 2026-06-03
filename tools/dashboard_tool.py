"""Dashboard Tool — generates a formatted Project Briefing terminal report.

Consolidates information from the task registry, knowledge base, event logs,
and git repository into a single human-readable overview.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from bootstrap import (
    BASE_DIR,
    EVENT_LOG_PATH,
    TASK_REGISTRY_PATH,
    append_event,
    load_json,
)

from tools.git_tool import git_current_branch, git_status
from tools.knowledge_tool import _all_insight_files, _parse_insight
from tools.llm_tool import query_llm


def _get_task_overview() -> Dict[str, Any]:
    """Return a formatted task overview from the task registry."""
    try:
        registry = load_json(TASK_REGISTRY_PATH, {})
        tasks = registry.get("tasks", [])
        total = len(tasks)
        counts: Dict[str, int] = {"done": 0, "pending": 0, "in_progress": 0}

        for task in tasks:
            status = task.get("status", "pending")
            if status in counts:
                counts[status] += 1

        lines = [
            "=== TASK OVERVIEW ===",
            f"  Total tasks: {total}",
            f"  Done:        {counts['done']}",
            f"  In Progress: {counts['in_progress']}",
            f"  Pending:     {counts['pending']}",
        ]

        return {
            "success": True,
            "output": "\n".join(lines),
            "error": None,
        }

    except Exception as e:
        append_event(
            "tool:dashboard",
            {"section": "task_overview", "success": False, "error": str(e)},
        )
        return {
            "success": False,
            "output": "=== TASK OVERVIEW ===\n  Unable to load task registry.",
            "error": str(e),
        }


def _get_recent_intelligence() -> Dict[str, Any]:
    """Return the 3 most recent insights from the knowledge base."""
    try:
        files = _all_insight_files()

        if not files:
            return {
                "success": True,
                "output": "=== RECENT INTELLIGENCE ===\n  No insights stored in the knowledge base.",
                "error": None,
            }

        insights = []
        for f in files:
            parsed = _parse_insight(f)
            if parsed:
                insights.append(parsed)

        recent = insights[-3:]
        lines = ["=== RECENT INTELLIGENCE ==="]

        for ins in recent:
            topic = ins.get("topic", "Untitled")
            summary = ins.get("summary", "")
            lines.append(f"  - [{topic}] {summary}")

        return {
            "success": True,
            "output": "\n".join(lines),
            "error": None,
        }

    except Exception as e:
        append_event(
            "tool:dashboard",
            {"section": "recent_intelligence", "success": False, "error": str(e)},
        )
        return {
            "success": True,
            "output": "=== RECENT INTELLIGENCE ===\n  Unable to load knowledge base.",
            "error": str(e),
        }


def _get_system_pulse() -> Dict[str, Any]:
    """Return a summary of the last 10 events from the event log."""
    try:
        if not EVENT_LOG_PATH.exists():
            return {
                "success": True,
                "output": "=== SYSTEM PULSE ===\n  No events recorded yet.",
                "error": None,
            }

        all_lines = EVENT_LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
        all_lines = [l for l in all_lines if l.strip()]

        if not all_lines:
            return {
                "success": True,
                "output": "=== SYSTEM PULSE ===\n  No events recorded yet.",
                "error": None,
            }

        recent_lines = all_lines[-10:]
        events: List[Dict[str, Any]] = []

        for line in recent_lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not events:
            return {
                "success": True,
                "output": "=== SYSTEM PULSE ===\n  No valid events to summarize.",
                "error": None,
            }

        event_text = "\n".join(
            json.dumps(ev, default=str) for ev in events
        )

        prompt = (
            "You are given the last 10 system events from an AI development tool. "
            "Summarize the system's recent activity in 2-3 short sentences. "
            "Focus on what the system has been doing. Return ONLY the summary text.\n\n"
            f"Events:\n{event_text}"
        )

        llm_result = query_llm(prompt, temperature=0.3)

        if llm_result["success"] and llm_result.get("content"):
            summary = llm_result["content"].strip()
        else:
            event_types = [ev.get("type", "unknown") for ev in events]
            summary = (
                f"Processed {len(events)} recent events. "
                f"Activity types: {', '.join(set(event_types))}."
            )

        lines = [
            "=== SYSTEM PULSE ===",
            f"  {summary}",
            f"  ({len(events)} recent events analyzed)",
        ]

        append_event(
            "tool:dashboard",
            {"section": "system_pulse", "success": True, "events_analyzed": len(events)},
        )

        return {
            "success": True,
            "output": "\n".join(lines),
            "error": None,
        }

    except Exception as e:
        append_event(
            "tool:dashboard",
            {"section": "system_pulse", "success": False, "error": str(e)},
        )
        return {
            "success": True,
            "output": "=== SYSTEM PULSE ===\n  Unable to analyze recent events.",
            "error": str(e),
        }


def _get_git_status_section() -> Dict[str, Any]:
    """Return current branch and git status summary."""
    try:
        branch_result = git_current_branch()
        status_result = git_status()

        branch = "unknown"
        if branch_result["success"]:
            branch = branch_result["output"].strip()

        status_text = "clean"
        if status_result["success"]:
            porcelain = status_result["output"].strip()
            if porcelain:
                lines = [l for l in porcelain.split("\n") if l.strip() and not l.startswith("##")]
                if lines:
                    status_text = "has uncommitted changes"
                else:
                    status_text = "clean"
            else:
                status_text = "clean"
        else:
            status_text = "unable to determine"

        lines = [
            "=== GIT STATUS ===",
            f"  Branch: {branch}",
            f"  Status: {status_text}",
        ]

        append_event(
            "tool:dashboard",
            {"section": "git_status", "success": True, "branch": branch, "status": status_text},
        )

        return {
            "success": True,
            "output": "\n".join(lines),
            "error": None,
        }

    except Exception as e:
        append_event(
            "tool:dashboard",
            {"section": "git_status", "success": False, "error": str(e)},
        )
        return {
            "success": True,
            "output": "=== GIT STATUS ===\n  Unable to retrieve git status.",
            "error": str(e),
        }


def generate_dashboard() -> Dict[str, Any]:
    """Generate the full Project Briefing dashboard.

    Orchestrates all four dashboard sections and returns a single
    formatted output string.

    Returns:
        {"success": bool, "output": str, "error": str | None}
    """
    try:
        sections = []
        errors: List[str] = []

        task_result = _get_task_overview()
        sections.append(task_result["output"])
        if task_result.get("error"):
            errors.append(f"Task overview: {task_result['error']}")

        intel_result = _get_recent_intelligence()
        sections.append("")
        sections.append(intel_result["output"])
        if intel_result.get("error"):
            errors.append(f"Recent intelligence: {intel_result['error']}")

        pulse_result = _get_system_pulse()
        sections.append("")
        sections.append(pulse_result["output"])
        if pulse_result.get("error"):
            errors.append(f"System pulse: {pulse_result['error']}")

        git_result = _get_git_status_section()
        sections.append("")
        sections.append(git_result["output"])
        if git_result.get("error"):
            errors.append(f"Git status: {git_result['error']}")

        header = "=" * 50
        briefing = "\n".join([
            header,
            "  PROJECT BRIEFING",
            header,
            "",
        ]) + "\n".join(sections) + "\n" + header

        append_event(
            "tool:dashboard",
            {"action": "generate_dashboard", "success": True, "sections": 4},
        )

        return {
            "success": True,
            "output": briefing,
            "error": "; ".join(errors) if errors else None,
        }

    except Exception as e:
        append_event(
            "tool:dashboard",
            {"action": "generate_dashboard", "success": False, "error": str(e)},
        )
        return {
            "success": False,
            "output": "",
            "error": str(e),
        }


if __name__ == "__main__":
    result = generate_dashboard()
    print(result["output"])
    if result.get("error"):
        print(f"\n[Warnings]: {result['error']}")
