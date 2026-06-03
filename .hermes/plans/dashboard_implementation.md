# Dashboard Tool Implementation Plan

## Overview
Implement `tools/dashboard_tool.py` — a terminal-facing tool that produces a formatted "Project Briefing" consolidating data from the task registry, knowledge base, event logs, and git repository.

## Components

### 1. Task Overview (`_get_task_overview`)
- Load `task_registry.json` via `bootstrap.load_json`
- Count total tasks and tasks by status: `done`, `pending`, `in_progress`
- Format as a summary section with bullet points

### 2. Recent Intelligence (`_get_recent_intelligence`)
- Use `knowledge_tool._all_insight_files()` to get insight files
- Parse up to 3 most recent insights using `knowledge_tool._parse_insight()`
- Format topic + summary for each
- Gracefully handle empty knowledge base

### 3. System Pulse (`_get_system_pulse`)
- Read last 10 lines from `logs/event_log.jsonl`
- Feed event data to `llm_tool.query_llm()` for natural language synthesis
- Fallback to raw event listing when LLM is unavailable

### 4. Git Status (`_get_git_status`)
- Use `git_tool.git_current_branch()` for branch name
- Use `git_tool.git_status()` for porcelain status
- Interpret porcelain output: clean vs has changes

### 5. Main Function (`generate_dashboard`)
- Orchestrate all 4 sections
- Combine into a single formatted output with headers and separators
- Return standard `{"success": bool, "output": str, "error": str | None}` dict

## Testing Strategy
- Mock all external dependencies: `bootstrap.load_json`, `knowledge_tool`, `llm_tool`, `git_tool`
- Test each section function in isolation
- Test graceful degradation when components are empty/unavailable
- Test the main `generate_dashboard` orchestration
- Verify return format compliance

## Return Format
All public functions return: `{"success": bool, "output": str, "error": str | None}`
