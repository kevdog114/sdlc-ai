# SDLC AI

An autonomous, AI-powered software development lifecycle (SDLC) system designed to function as a complete software scrum team.

## Overview

The SDLC AI system takes high-level requirements and manages the entire process from conception to deployment and monitoring. It automates the roles traditionally held by a business analyst, software developer, test analyst, release manager, and infrastructure analyst.

## Core Workflow

1.  **Requirement Breakdown**: The system accepts high-level requirements and uses an AI model to decompose them into:
    *   Comprehensive technical requirements.
    *   **Initial Interface Specification (e.g., OpenAPI/GraphQL)**: Defining the "contract" between components.
    *   Specific deliverables.
    *   Granular individual tasks.

2.  **Autonomous Execution (The Developer Loop)**:
    *   Uses **OpenCode** with a selected AI model to execute tasks in the optimal order.
    *   Each task is completed by an agent acting as a specialized Software Developer (UI or Backend).
    *   **Contract-First Implementation**: Developers must implement code according to the established Interface Specification.

3.  **Quality Assurance & Stage Gates**:
    *   **Test Analyst Agent**: Identifies stage-gate requirements for every task, such as creating and validating automated UI tests.
    *   **Architectural Consistency Agent**: An automated agent that checks all changes against the defined system architecture to ensure consistency and prevent technical debt.

4.  **Automated Deployment (The Release Manager)**:
    *   Once deployment information is provided (e.g., Docker Compose configurations, server credentials), the **Release Manager Agent** takes over.
    *   It automatically builds and deploys the application.
    *   **Infrastructure Analyst Agent**: Monitors logs for errors, performance regressions, or issues that need immediate attention.

5.  **Feedback & Reporting**:
    *   The system generates a daily progress summary.
    *   Sends notifications to the user via preferred channels (e.g., Telegram) if human intervention is required to resolve an error or provide clarification.

## Agent Roles & Responsibilities

The system operates as a multi-agent orchestration where each agent is specialized in a specific domain of the SDLC.

| Agent | Primary Responsibility | Key Tasks |
| :--- | :--- | :--- |
| **Business Analyst (BA)** | Requirement Decomposition | • Transform high-level human intent into technical requirements.<br>• Decompose requirements into actionable deliverables.<br>• Generate and maintain the master task list. |
| **Solution Architect** | System Design & Integrity | • Define the high-level technical architecture (tech stack, data models).<br>• Maintain architectural consistency across all code changes.<br>• Approve/Reject structural changes proposed by developers. |
| **Software Developer** | Task Execution | • Execute granular tasks using OpenCode and selected AI models.<br>• Implement features and bug fixes according to requirements.<br>• Ensure code adheres to established patterns and standards. |
| **Test Analyst** | Quality Assurance | • Define stage-gate criteria for every task (e.g., unit, integration, UI tests).<br>• Create automated test suites.<br>• Validate that completed tasks meet all acceptance criteria before handoff. |
| **Infrastructure Analyst** | Environment & Monitoring | • Monitor system logs, resource usage, and error rates.<br>• Detect deployment failures or performance regressions.<br>• Alert the Release Manager of anomalies. |
| **Release Manager** | Deployment & Orchestration | • Execute build and deployment pipelines (e.g., Docker).<br>• Manage versioning and deployment to specific environments.<br>• Coordinate human intervention when blockers arise. |

## Operational Protocols

### 1. Completion & Sign-off Pipeline
To ensure quality, every task must pass through a strict three-key verification process:
1.  **Developer**: Completes the code and moves status to `Pending Verification`.
2.  **Test Analyst**: Executes automated suites. If successful $\rightarrow$ `Testing Passed`. If failed $\rightarrow$ `Rejected` (with error logs attached).
3.  **Solution Architect**: Performs structural review. If approved $\rightarrow$ `Completed`. If rejected $\rightarrow$ `Rejected` (design violation notice).

### 2. Model Routing & Intelligence Tiering
The system optimizes for speed, cost, and reasoning capabilities:
*   **Manual Mode**: Users can specify a dedicated model for each agent in the project configuration.
*   **Auto Mode**: An Orchestrator evaluates task complexity to route work:
    *   *High Complexity (Design/Decomposition)* $\rightarrow$ High-reasoning models (e.g., DeepSeek-V4).
    *   *Medium Complexity (Implementation)* $\rightarrow$ Specialized coding models (e.g., Qwen-Coder).
    *   *Low Complexity (Log parsing/Reporting)* $\rightarrow$ Lightweight, fast models.

### 3. Security & Secret Management
Secrets are never stored in plain text within the project repository or the State Store.
*   **Tool-Gated Access**: Agents interact with secrets via a specialized `get_secret(key)` tool.
*   **Backend Storage**: Actual credentials reside in a secure, centralized database, retrieved only into volatile memory during task execution.

### 5. Contract-First Development & Interface Integrity

To prevent integration errors and "interface drift," the system adheres to a contract-first methodology using local specification files (e.g., `openapi.yaml`, `schema.graphql`) as the source of truth.

*   **Spec Generation**: During the Requirement Breakdown, the BA/Architect agents generate an initial interface specification.
*   **Implementation**: Developers are strictly required to build features that satisfy the existing specification.
*   **Handling Interface Gaps**: If a developer identifies a need for an additional property or endpoint (an "Interface Gap"):
    1.  The agent **must not** simply change the code to make it work.
    2.  The agent attempts to update the local specification file.
    3.  If the change requires a task outside their current scope (e.g., a Frontend dev needing a Backend change), they must mark the task as `Blocked: Interface Gap` and elevate the requirement to the **Orchestrator** to trigger the necessary cross-role work.

The system manages work using a digital **Kanban Board**, providing real-time visibility into the project status.

### 1. Story-Based Workflow
Work is organized into "Stories" (User Stories/Requirements). As agents move through the lifecycle:
*   **Completion Notes**: Upon completing a story, the assigned agent must provide a summary of their work, including technical implementation details and any side effects.
*   **Status Updates**: The Kanban board tracks the movement of stories through stages: `Backlog` $\rightarrow$ `In Progress` $\rightarrow$ `Testing` $\rightarrow$ `Architect Review` $\rightarrow$ `Done`.

### 2. Automated Daily Reporting
A scheduled cron job aggregates all activity from the previous 24 hours:
*   It pulls all "Completed" stories.
*   It synthesizes the agents' completion notes into a coherent narrative.
*   It presents this as a structured report (e.g., via Telegram) for the user, ensuring transparency without requiring constant monitoring.

### 3. User Visibility
The Kanban board and current project state are exposed through a user-facing interface, allowing the human to monitor progress, review agent notes, and adjust autonomy levels on the fly.

### 1. Proactive Error Reporting
Deployed applications can be configured to send proactive "Heartbeat/Error" notifications via webhooks directly to the Scrum Team. An unhandled exception in the app triggers an immediate event that the **Business Analyst** uses to generate a new bug report and task.

### 2. Passive Log Analysis
The **Infrastructure Analyst** performs periodic scans of system logs to identify:
*   Non-critical warnings or "silent" failures.
*   Performance regressions (e.g., increased latency).
*   Anomalous patterns that do not trigger explicit errors but indicate instability.

## Core State Management

To ensure consistency across all agents, the system utilizes a centralized **Project State Store**. This acts as the "Single Source of Truth" for the entire lifecycle.

### 1. Knowledge Base (The "Brain")
This section contains the qualitative data that guides decision-making:
*   **Human Vision:** The original high-level intent and constraints provided by the user.
*   **Technical Requirements:** Structured specifications generated by the Business Analyst.
*   **System Architecture:** The blueprint (design patterns, schema, component maps) maintained by the Solution Architect.

### 2. Operational Context (The "Hands")
This section contains the quantitative data required for execution:
*   **Project Configuration:** Initial setup details provided by the human, including:
    *   Local and upstream Git repository paths/URLs.
    *   Deployment targets (e.g., Docker Compose files, server IPs, credentials).
    *   Environment variables and system-specific constraints.
*   **Task & Bug Registry:** A dynamic list of all tasks and issues, tracking:
    *   `ID`, `Description`, and `Dependencies`.
    *   `Status` (Pending, In Progress, Testing, Completed, Blocked).
    *   `Assigned Agent`.
    *   `Verification Artifacts` (links to test results or logs).

### 3. Event Log (The "Memory")
A chronological record of all agent actions, tool outputs, and system changes to allow for auditing, debugging, and "re-planning" if an error occurs.
