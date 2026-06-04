"""Knowledge/Context Tool — long-term memory via local knowledge repository.

Provides store, retrieve, and list operations over a directory-based
knowledge store (`.knowledge/`).  Uses `llm_tool` for summary generation
and semantic fallback search, and logs all access via `append_event`.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bootstrap import BASE_DIR, append_event

from tools.llm_tool import query_llm

KNOWLEDGE_DIR = BASE_DIR / ".knowledge"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_filename(name: str) -> str:
    """Convert a topic string into a safe filename component."""
    sanitized = re.sub(r"[^\w\s-]", "", name).strip().lower()
    sanitized = re.sub(r"[\s_]+", "-", sanitized)
    return sanitized or "untitled"


def _generate_summary(content: str) -> str:
    """Ask the LLM for a one-sentence summary.  Falls back to a
    truncated prefix when the LLM is unavailable."""
    prompt = (
        "Summarize the following text in a single short sentence "
        "(max 80 characters). Return ONLY the summary, nothing else.\n\n"
        f"{content}"
    )
    result = query_llm(prompt, temperature=0.3)
    if result["success"] and result.get("content"):
        summary = result["content"].strip()[:80]
        return summary
    return content[:80]


def _ensure_knowledge_dir() -> Path:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    return KNOWLEDGE_DIR


def _all_insight_files() -> List[Path]:
    """Return all `.md` insight files in the knowledge directory."""
    if not KNOWLEDGE_DIR.is_dir():
        return []
    return sorted(KNOWLEDGE_DIR.glob("*.md"))


def _parse_insight(file_path: Path) -> Dict[str, Any]:
    """Parse a markdown insight file into a structured dict."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return {}

    meta_block = {}
    content_lines: List[str] = []
    in_meta = False

    for line in text.split("\n"):
        if line.startswith("---"):
            in_meta = not in_meta
            continue
        if in_meta:
            if ":" in line:
                key, _, val = line.partition(":")
                meta_block[key.strip().lower()] = val.strip()
        else:
            content_lines.append(line)

    body = "\n".join(content_lines).strip()
    return {
        "file": str(file_path),
        "topic": meta_block.get("topic", ""),
        "summary": meta_block.get("summary", ""),
        "created_at": meta_block.get("created_at", ""),
        "content": body,
    }


def _keyword_score(query: str, insight: Dict[str, Any]) -> float:
    """Simple keyword overlap score between query and insight fields."""
    q_words = set(query.lower().split())
    if not q_words:
        return 0.0

    searchable = f"{insight.get('topic', '')} {insight.get('summary', '')} {insight.get('content', '')}".lower()
    s_words = set(searchable.split())

    overlap = q_words & s_words
    if not s_words:
        return 0.0

    return len(overlap) / len(q_words)


def store_insight(topic: str, content: str) -> Dict[str, Any]:
    """Store a structured insight into the local knowledge repository.

    Creates a markdown file with a YAML-like front matter block for
    metadata (topic, summary, created_at) followed by the raw content.

    Returns:
        {"success": bool, "output": str, "error": str | None}
    """
    try:
        if not topic or not topic.strip():
            append_event(
                "tool:knowledge_access",
                {"action": "store", "topic": topic, "success": False, "error": "empty topic"},
            )
            return {"success": False, "output": "", "error": "Topic cannot be empty."}

        if not content or not content.strip():
            append_event(
                "tool:knowledge_access",
                {"action": "store", "topic": topic, "success": False, "error": "empty content"},
            )
            return {"success": False, "output": "", "error": "Content cannot be empty."}

        summary = _generate_summary(content)
        ts = _now()
        safe_name = _sanitize_filename(topic)

        # Append a counter if name collision
        file_path = _ensure_knowledge_dir() / f"{safe_name}.md"
        counter = 1
        while file_path.exists():
            file_path = _ensure_knowledge_dir() / f"{safe_name}-{counter}.md"
            counter += 1

        markdown = (
            "---\n"
            f"topic: {topic}\n"
            f"summary: {summary}\n"
            f"created_at: {ts}\n"
            "---\n\n"
            f"{content}\n"
        )
        file_path.write_text(markdown, encoding="utf-8")

        append_event(
            "tool:knowledge_access",
            {
                "action": "store",
                "topic": topic,
                "file": str(file_path),
                "success": True,
            },
        )

        return {
            "success": True,
            "output": f"Insight stored: {file_path.name}",
            "error": None,
        }

    except Exception as e:
        append_event(
            "tool:knowledge_access",
            {
                "action": "store",
                "topic": topic,
                "success": False,
                "error": str(e),
            },
        )
        return {"success": False, "output": "", "error": str(e)}


def query_knowledge(query: str) -> Dict[str, Any]:
    """Search the knowledge repository and return the top 3 relevant insights.

    Uses keyword matching first; if fewer than 3 results have a score > 0,
    falls back to an LLM-based semantic ranking.

    Returns:
        {"success": bool, "output": str, "error": str | None}
    """
    try:
        if not query or not query.strip():
            append_event(
                "tool:knowledge_access",
                {"action": "query", "query": query, "success": False, "error": "empty query"},
            )
            return {
                "success": False,
                "output": "",
                "error": "Query cannot be empty.",
            }

        files = _all_insight_files()
        if not files:
            append_event(
                "tool:knowledge_access",
                {"action": "query", "query": query, "success": True, "count": 0},
            )
            return {
                "success": True,
                "output": "No insights stored in the knowledge base.",
                "error": None,
            }

        insights = [_parse_insight(f) for f in files]
        insights = [i for i in insights if i]  # drop parse failures

        # Keyword scoring
        scored: List[tuple] = [
            (_keyword_score(query, ins), ins) for ins in insights
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        # Take top 3
        top = scored[:3]

        # If keyword scores are all zero, try semantic fallback
        if all(s == 0.0 for s, _ in top):
            top = _semantic_fallback(query, insights)

        results = [ins for _, ins in top]
        output = _format_results(results)

        append_event(
            "tool:knowledge_access",
            {
                "action": "query",
                "query": query,
                "success": True,
                "count": len(results),
            },
        )

        return {
            "success": True,
            "output": output,
            "error": None,
        }

    except Exception as e:
        append_event(
            "tool:knowledge_access",
            {
                "action": "query",
                "query": query,
                "success": False,
                "error": str(e),
            },
        )
        return {"success": False, "output": "", "error": str(e)}


def _semantic_fallback(query: str, insights: List[Dict[str, Any]]) -> List[tuple]:
    """Ask the LLM to rank insights by relevance to the query.

    Returns a list of (score, insight) tuples, with 1.0 as the max
    synthetic score (order matters, not absolute value).
    """
    if not insights:
        return []

    insight_indexed = "\n\n".join(
        f"[{idx}] Topic: {i.get('topic', '')}\nSummary: {i.get('summary', '')}"
        for idx, i in enumerate(insights, 1)
    )

    prompt = (
        "Given the following query, rank the numbered insights by relevance "
        "(most relevant first). Return ONLY the sorted indices as a comma-"
        "separated list, e.g. '3,1,2'.\n\n"
        f"Query: {query}\n\n"
        f"Insights:\n{insight_indexed}"
    )

    result = query_llm(prompt, temperature=0)
    if result["success"] and result.get("content"):
        try:
            ordered = [int(x.strip()) for x in result["content"].split(",") if x.strip().isdigit()]
            ordered = [x for x in ordered if 1 <= x <= len(insights)]
            if ordered:
                scored: List[tuple] = []
                for rank, idx in enumerate(ordered[:3]):
                    scored.append((1.0 - rank * 0.25, insights[idx - 1]))
                # Fill remaining slots with unranked insights
                used = set(ordered[:3])
                for i, ins in enumerate(insights):
                    if i + 1 not in used and len(scored) < 3:
                        scored.append((0.0, ins))
                return scored
        except (ValueError, IndexError):
            pass

    # Fallback: return first 3 by creation order
    return [(0.0, ins) for ins in insights[:3]]


def _format_results(results: List[Dict[str, Any]]) -> str:
    """Format a list of insight dicts into a readable string."""
    if not results:
        return "No matching insights found."

    parts: List[str] = []
    for i, ins in enumerate(results, 1):
        topic = ins.get("topic", "Untitled")
        summary = ins.get("summary", "")
        content = ins.get("content", "")
        parts.append(f"### Result {i}: {topic}")
        if summary:
            parts.append(f"Summary: {summary}")
        parts.append(f"Content:\n{content}")
        parts.append("")

    return "\n".join(parts)


def list_topics() -> Dict[str, Any]:
    """Return all unique topics currently stored in the knowledge base.

    Returns:
        {"success": bool, "topics": list[str], "error": str | None}
    """
    try:
        files = _all_insight_files()
        topics: List[str] = []
        for f in files:
            ins = _parse_insight(f)
            topic = ins.get("topic")
            if topic and topic not in topics:
                topics.append(topic)

        append_event(
            "tool:knowledge_access",
            {
                "action": "list_topics",
                "success": True,
                "count": len(topics),
            },
        )

        return {
            "success": True,
            "topics": topics,
            "error": None,
        }

    except Exception as e:
        append_event(
            "tool:knowledge_access",
            {
                "action": "list_topics",
                "success": False,
                "error": str(e),
            },
        )
        return {"success": False, "topics": [], "error": str(e)}


if __name__ == "__main__":
    print("[test] Store insight …")
    print(json.dumps(store_insight("API Design", "Use RESTful endpoints with JSON payloads."), indent=2))
    print()
    print("[test] Query knowledge …")
    print(json.dumps(query_knowledge("REST API"), indent=2))
    print()
    print("[test] List topics …")
    print(json.dumps(list_topics(), indent=2))
