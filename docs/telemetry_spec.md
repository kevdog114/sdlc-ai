# Agent Telemetry Specification (v1)

## Overview
To achieve "live radar" observability, every autonomous agent within the AOS must emit periodic telemetry updates. This allows the Pulse Server and Radar Dashboard to visualize the state of the entire ecosystem in real-time.

## Communication Pattern
Agents will use a **Push model**. Every iteration or significant event (tool call, error, mission completion) triggers a JSON payload sent via a lightweight HTTP POST request to the Pulse Server's `/telemetry` endpoint.

## Telemetry Payload Schema (JSON)

### 1. Core Metadata (The "Who" and "Where")
- `agent_id`: Unique UUID of the running agent process.
- `persona`: The name of the persona being used (e.g., `expert_dev`).
- `job_id`: If the agent was spawned by an `agent_manager`, this links it to the parent job.
- `timestamp`: ISO 8601 timestamp of the event.

### 2. Lifecycle State (The "What")
`state` is one of: `initializing`, `thinking`, `acting`, `observing`, `completed`, `failed`, or `interrupted`.

### 3. The "Heartbeat" Payload
Depending on the `state`, the payload includes specific details:

#### **State: `thinking`**
- `current_goal`: The user prompt/mission goal.
- `iteration`: Current loop count (e.g., 4).
- `last_thought`: The last snippet of reasoning from the LLM.

#### **State: `acting`**
- `tool_name`: The name of the tool being invoked.
- `tool_arguments`: A sanitized version of the arguments passed to the tool.

#### **State: `observing`**
- `tool_result_summary`: A truncated (first 100 chars) summary of what the tool returned.

#### **State: `failed` / `completed`**
- `exit_code`: The integer exit code.
- `error_message`: If failed, the exception or error string.
- `final_output`: A summary of the final result.

## Example Payload (Acting State)
```json
{
  "agent_id": "550e8400-e29b-41d4-a716-446655440000",
  "persona": "expert_dev",
  "job_id": "8086dadc",
  "timestamp": "2026-06-03T19:05:01Z",
  "state": "acting",
  "iteration": 3,
  "tool_name": "file_manager",
  "tool_arguments": {"function": "write_file", "params": {"path": "test.txt", "content": "hello"}}
}
```

## Integration Strategy
1.  **`tools/telemetry.py`**: A new tool module containing a `TelemetryClient`. This client will handle the HTTP requests to the Pulse Server, including retries and batching if necessary.
2.  **Runtime Injection**: The `AgentRuntime` in `core/runtime.py` will be modified to instantiate this client and automatically emit events at each step of the ReAct loop.
3.  **Pulse Server Extension**: The existing `pulse_server.py` (running on port 8081) will be updated with a `/telemetry` route to receive, store, and broadcast these updates via its existing WebSocket channel (`/radar-ws`).
