# Orchestration Implementation Plan

## Overview

This plan details the implementation of the Multi-Agent Orchestration Framework, enabling the seed agent to decompose high-level goals into subtasks and delegate them to specialized agent personas.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Orchestrator                       │
│  (decomposes goals → assigns roles → monitors)       │
├──────────┬──────────┬──────────┬────────────────────┤
│Architect │Developer │   QA    │   Researcher        │
│ persona  │ persona  │ persona  │   persona           │
└────┬─────┴────┬─────┴────┬─────┴────────┬───────────┘
     │          │          │              │
     └──────────┴──────────┴──────────────┘
                  │
     ┌────────────┼────────────┐
     │            │            │
  registry_tool  knowledge_tool  llm_tool
  (task tracking) (shared ctx)  (reasoning)
```

## Components

### 1. `roles/` — Agent Persona Definitions

JSON files defining each specialist role:

- `roles/architect.json`: System design, task decomposition, oversight
- `roles/developer.json`: Code implementation, debugging, refactoring
- `roles/qa.json`: Test writing, verification, quality assurance
- `roles/researcher.json`: Information gathering, analysis, knowledge synthesis

Each persona defines:
- `name`: Unique identifier
- `system_prompt`: Personality and behavioral constraints
- `preferred_model`: Model hint (e.g., "local")
- `capabilities`: List of operations the role excels at

### 2. `tools/orchestrator_tool.py` — Core Orchestration Logic

Key functions:

- `load_role(role_name)`: Load a persona definition from `roles/`
- `list_roles()`: Return all available role names
- `decompose_goal(goal)`: Use LLM to break a high-level goal into atomic subtasks with role assignments
- `delegate_task(task, role_name)`: Create a registry entry, inject the role's system prompt, and execute via `query_llm`
- `monitor_tasks()`: Check all in-progress tasks via registry, detect failures
- `handle_failure(task, error)`: Decide retry/re-assign/escalate based on failure count
- `run_orchestration(goal)`: End-to-end orchestration loop: decompose → delegate → monitor → collect results

### 3. `tests/test_orchestration.py` — Test Suite

Test classes:

- `TestRoleDefinitions`: Validate all role JSON files load correctly and have required fields
- `TestDecomposeGoal`: Test goal decomposition with mocked LLM responses
- `TestDelegateTask`: Test task creation, role assignment, and LLM invocation
- `TestMonitorTasks`: Test in-progress detection and failure detection
- `TestHandleFailure`: Test retry logic, re-assignment, and escalation paths
- `TestRunOrchestration`: Full end-to-end orchestration simulation
- `TestFailureRecovery`: Simulate subagent failures and verify recovery behavior

## Data Flow

1. **Goal Ingestion**: Orchestrator receives a high-level goal string
2. **Decomposition**: `decompose_goal` calls LLM with a structured prompt, returns JSON array of `{task, role, dependencies}`
3. **Registry Entries**: Each subtask creates a registry entry via `add_task` with the assigned role name
4. **Execution**: `delegate_task` loads the role's `system_prompt`, calls `query_llm`, captures result
5. **Knowledge Logging**: Significant findings stored via `store_insight`
6. **Monitoring**: `monitor_tasks` polls registry for status changes, detects failures
7. **Recovery**: `handle_failure` implements retry (up to max_retries), re-assign to different role, or escalate
8. **Completion**: Results aggregated and returned to the caller

## Error Handling Strategy

- **Retry**: Same role, same task, up to `max_retries` (default: 2)
- **Re-assign**: Different role, same task, when retry count exhausted
- **Escalate**: Mark task as `escalated`, log to knowledge base, continue with remaining tasks

## Dependencies

- `bootstrap`: `add_task`, `update_task_status`, `append_event`, `load_json`, `save_json`
- `tools.registry_tool`: `get_pending_tasks`, `get_task_by_id`, `update_task_status`, `create_new_task`
- `tools.knowledge_tool`: `store_insight`, `query_knowledge`
- `tools.llm_tool`: `query_llm`
