# High-Fidelity Persona Specification (v2)

## Overview
The AOS Persona Specification defines the cognitive and behavioral boundaries of an autonomous agent. Unlike simple system prompts, these personas provide structured metadata that can be used by the `AgentRuntime` to tune LLM parameters, restrict tool access, and guide communication styles.

## Schema Definition (YAML)

### 1. Identity Metadata
- `name`: Unique identifier for the persona (e.g., `senior_dev`).
- `title`: Human-readable title (e.g., `Senior Full-Stack Engineer`).
- `description`: A brief summary of what this agent is for.
- `version`: Semantic versioning of the persona definition.

### 2. Cognitive Profile
- `expertise`: List of specialized domains (e.g., `[python, docker, security]`).
- `reasoning_style`: How the agent approaches problems (`deep_dive`, `rapid_prototype`, `formal_verification`).
- `communication_style`: Tone and format for outputs (`concise_technical`, `verbose_explanatory`, `collaborative_friendly`).

### 3. Operational Constraints (The "Guardrails")
- `allowed_tools`: A whitelist of tool namespaces the agent is permitted to use (e.g., `[file_manager, shell_executor]`). *Crucial for security and preventing agent drift.*
- `max_loop_depth`: Maximum number of recursive subagent spawns this persona can initiate.
- `risk_threshold`: A numeric value (0-1) determining how much "autonomous risk" the agent can take before requiring human intervention (via a `clarify` call).

### 4. Behavioral Instructions
- `system_prompt`: The core directive for the LLM.
- `rules`: A list of explicit, imperative rules (e.g., `- Always verify file existence before writing.`).
- `preferred_format`: Preferred output structure (e.g., `json`, `markdown`, `bullet_points`).

## Example Implementation: `personas/senior_dev.yaml`

```yaml
name: senior_dev
title: Senior Full-Stack Engineer
description: Expert in Python, React, and containerized environments.
version: 1.0.0

cognitive_profile:
  expertise: [python, react, docker, postgres]
  reasoning_style: deep_dive
  communication_style: concise_technical

operational_constraints:
  allowed_tools: [file_manager, shell_executor, registry_tool, agent_state]
  max_loop_depth: 2
  risk_threshold: 0.3

behavioral_instructions:
  system_prompt: |
    You are a Senior Full-Stack Engineer at sdlc-ai. 
    Your mission is to write production-ready, tested code.
  rules:
    - Always include type hints in Python code.
    - Always verify that a directory exists before attempting to list it.
    - Use shell commands only when necessary; prefer file_manager for simple I/O.
  preferred_format: markdown
```

## Integration Plan
1.  **`tools/persona_manager.py`**: A new tool to load, validate (using `jsonschema`), and retrieve personas.
2.  **Runtime Update**: The `AgentRuntime` will consume this full object instead of just a raw string.
3.  **Tool Filtering**: During runtime, the `tool_registry` will be filtered against the persona's `allowed_tools` list to ensure strict adherence to constraints.
