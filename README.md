# SDLC AI

An autonomous, AI-powered software development lifecycle system that operates like a **scrum team**:
it turns a high-level project description into an estimated product backlog, runs the work in
**sprints** through a real quality gate, presents each increment for your acceptance, and improves
itself with a retrospective every sprint.

> **Status:** the core loop is implemented and tested (488 passing tests). It runs against a local
> LLM (LM Studio / any OpenAI-compatible endpoint) and, for code implementation, an
> [OpenCode](https://github.com/sst/opencode) server. See [docs/](docs/) for the full review,
> architecture, and roadmap. Deployment beyond localhost requires an API token (the server refuses
> to bind to a public interface without one).

## How it works

Work is split into **definition** and **execution** — the difference between a fire-once script and
a team you keep a backlog with:

1. **Define.** You submit a project description. A *Business Analyst* agent refines the
   requirements and asks clarifying questions (via the dashboard or Telegram); a *Solution
   Architect* agent designs the architecture and a contract-first interface spec; then the work is
   decomposed into **estimated, project-scoped user stories** (story points on the 1/2/3/5/8 scale)
   that land in a durable **product backlog**. Nothing is built yet.
2. **Plan a sprint.** Stories are selected from the backlog — dependency- and priority-aware, under
   an optional point capacity — into a proposed sprint with a goal.
3. **Run the sprint.** Starting a sprint (your approval) executes its stories. Each task goes
   through the **three-key Definition of Done**: a *Developer* implements it (via OpenCode), a
   *QA* gate **runs the project's real tests** and reviews the result, and an *Architect* gate
   checks it against the architecture and interface contract. Failing work is retried with the
   rejection fed back in, then — after a bounded number of tries — **blocked for a human** rather
   than looping or silently failing.
4. **Review & accept.** At sprint review you accept or reject each delivered story. Rejections
   return to the backlog with your notes attached (and feed the next attempt). Only accepted points
   count toward **velocity**.
5. **Retrospective.** The system mines the sprint's event log for failure patterns and proposes up
   to three concrete process changes for your approval.
6. **Iterate.** Follow-up work enters as a **change request** — the BA sees the existing
   requirements, architecture, and story ledger and appends *new* stories, rather than rebuilding
   from scratch.

A live **dashboard** (the "pulse" server + `radar.html`) shows the kanban board, the backlog, and
sprints, and lets you drive the whole loop from the browser.

## Quickstart

Requires Python ≥ 3.10.

```bash
# 1. Install
pip install -e ".[dev]"          # editable + test deps; or `pip install .`

# 2. Point it at your LLM (see Configuration below)
cp config.example.yaml config.yaml   # then edit
#    …or export env vars instead

# 3. Initialize the local state store
sdlc-bootstrap

# 4. Start the dashboard (loopback by default) and open http://127.0.0.1:8080
sdlc-pulse
```

From the dashboard you can create a project, answer clarifications, plan and start a sprint, accept
stories, and read the retrospective. To drive it headless instead:

```bash
sdlc-plan "Build a REST API for a todo app with add/list/delete"   # define the backlog
# then use the /api/sprints/* endpoints, or the sprint_tool functions, to run it.
```

For **code implementation** you also need an OpenCode server reachable (default
`http://127.0.0.1:4096`); the developer path fails loudly with instructions if it isn't running.
Non-developer agents (BA, architect, QA reasoning) only need the LLM endpoint.

## Configuration

Configuration comes from `config.yaml` at the repo root (git-ignored) and/or environment variables.
Secrets are never read from the repo — they resolve via env or a git-ignored `.secrets/secrets.json`
through the `get_secret()` tool.

```yaml
# config.yaml
llm:
  base_url: "http://localhost:1234/v1"   # LM Studio / any OpenAI-compatible endpoint
  default_model: "your-loaded-model-id"  # must match what your server actually serves
  # api_key: set via env SDLCAI_SECRET_LLM_API_KEY or .secrets, NOT here
  timeout: 600
```

| Variable | Purpose |
| :--- | :--- |
| `SDLCAI_CONFIG` | Path to the config file (default: `./config.yaml`) |
| `SDLCAI_SECRET_LLM_API_KEY` | LLM API key (for hosted endpoints) |
| `SDLCAI_API_TOKEN` | Dashboard/API auth token — **required** to bind beyond loopback |
| `SDLCAI_HOST` | Server bind host (default `127.0.0.1`) |
| `SDLCAI_CORS_ORIGINS` | Comma-separated allowed origins (default: none) |
| `PULSE_SERVER_URL` | Where spawned agents post telemetry (default `http://127.0.0.1:8080`) |
| `OPENCODE_BIN` / `OPENCODE_PORT` | OpenCode binary name / port |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram notifications (optional) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs allowed to answer clarifications |

## Security

The dashboard binds to **loopback by default** and refuses to bind to a public interface unless
`SDLCAI_API_TOKEN` is set; when a token is set, every API call and the WebSocket require it. Agent
tools are constrained — file access is confined to the project directories, git and shell run without
`shell=True` injection surface, and network access is limited to http/https. This is safe for
single-operator local use. **Multi-tenant or internet-facing deployment additionally requires
sandboxing the shell/code-execution tools** (containers, network policy) — see
[docs/REVIEW.md](docs/REVIEW.md) P1-1.

## Running the tests

```bash
pip install -e ".[dev]"
pytest            # 488 passing; hermetic (writes only to tmp dirs)
```

CI runs the suite on every push across Python 3.10–3.12.

## Roles

| Agent | Responsibility |
| :--- | :--- |
| **Business Analyst** | Requirement refinement, clarification questions, story decomposition, change requests |
| **Solution Architect** | Architecture, interface specs, the architect quality gate |
| **Software Developer** | Task implementation via OpenCode, contract-first |
| **QA / Test Analyst** | Runs the real tests, the QA quality gate |
| **Orchestrator (Scrum Master)** | Sprint loop, gate enforcement, retries, human escalation |
| **Release Manager / Infrastructure Analyst** | Deployment & monitoring *(defined; implementation is on the roadmap)* |

## Documentation

- [docs/REVIEW.md](docs/REVIEW.md) — full code review and prioritized findings
- [docs/SCRUM_PROCESS.md](docs/SCRUM_PROCESS.md) — the scrum operating model
- [docs/AGENTS_AND_PROMPTS.md](docs/AGENTS_AND_PROMPTS.md) — agent/prompt design & local-model notes
- [docs/INTAKE_AND_TRACKING.md](docs/INTAKE_AND_TRACKING.md) — intake & backlog tracking analysis
- [docs/ROADMAP.md](docs/ROADMAP.md) — phased plan and what remains

## State & storage

State lives in local JSON under `state/` (task/story/sprint registries, project state,
clarifications), an append-only event log under `logs/event_log.jsonl` (the audit trail and the
substrate the retrospective mines), and per-project working directories. All of it is git-ignored.
