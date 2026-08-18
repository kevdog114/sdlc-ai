# Repository Review — 2026-08-18

**Reviewed:** `feature/project-dashboard` @ `7ee5435` (75 files, ~14k lines of Python + one Vue SPA).
**Request:** "Review this repo. I want it to work like a scrum team for developing projects."
**Method:** direct read of the core (`bootstrap.py`, `core/runtime.py`, `core/orchestrator_daemon.py`)
plus four parallel deep-dive reviews (orchestration/work management, integrations/UI, roles/entry
points, and the test suite) and a full test run.

---

## Verdict

You have built much more than the README-only baseline suggests: a real multi-agent engine with a
ReAct runtime, a stage-gate pipeline, contract-first interface specs, a JSON state store with an
event log, a live dashboard, and a Telegram human-in-the-loop. The bones of a good system are here,
and several of the hardest ideas (contract-first, three-key sign-off, an append-only event log) are
genuinely implemented, not just described.

Three things stand between this and the stated goal:

1. **It is a pipeline, not a scrum team.** The building blocks exist (stories, a board, gates, an
   event log) but the scrum *layer* — sprints, planning, review/acceptance, and above all the
   retrospective — is absent. A repo-wide search for `sprint|velocity|standup|retrospectiv|
   estimat|wip` returns **zero matches** in any `.py`, `.yaml`, or `.html` file. See
   [SCRUM_PROCESS.md](SCRUM_PROCESS.md) for the target operating model.

2. **The quality gate — your Definition of Done — is unsound.** The three-key sign-off is the
   feature that would make this trustworthy as a team, and right now it passes work the LLM
   actually rejected (details in P0-1). Until that is fixed, "Done" means nothing, and a scrum
   team whose "Done" means nothing is just an expensive way to generate unreviewed code.

3. **Nothing can currently verify itself.** The test suite fails to run (a collection error plus
   44 failures), so there is no safety net for any of the fixes below, and no CI to catch
   regressions. This is the first thing to fix, because everything else depends on it.

The rest of this document maps what exists, quantifies the scrum gap, and lists the defects in
priority order. The companion docs give the target process ([SCRUM_PROCESS.md](SCRUM_PROCESS.md))
and a phased plan ([ROADMAP.md](ROADMAP.md)).

---

## What the system does today

There are, in fact, **three independent execution engines**, which is itself the central
architectural problem (see P0-2):

| Engine | Entry | Runs quality gates? | State |
| :--- | :--- | :--- | :--- |
| **A. Pipeline** | `project_tool.submit_project` → `orchestrator_tool.delegate_task` → `stage_gate_tool` | **Yes** (the only one) | Live, default path |
| **B. Daemon** | `core/orchestrator_daemon.py` → `agent_manager.spawn_agent` → `core/runtime.py` | **No** — writes `done` directly | Live, alternate path |
| **C. Worker** | `agent_executor.py` → `worker.py` | **No** | Orphaned + crash-broken |

**The working path (Engine A)** flows: a human submits a project → the Business Analyst phase does
one LLM call to refine requirements and surface ambiguities → ambiguities become Telegram/dashboard
clarification questions that pause the run → the Architect phase designs the architecture and an
interface spec → an LLM decomposes the goal into stories and tasks → stories execute **strictly
sequentially in LLM-emitted order**, each task delegated to OpenCode (developers) or a direct LLM
call (other roles) → on success the task runs the stage-gate pipeline (QA → contract → architect) →
the story is marked done.

**Persistence** is plain JSON files under `state/` (`task_registry.json`, `story_registry.json`,
`project_state.json`, `clarifications.json`), an append-only `logs/event_log.jsonl`, and
per-project output written **outside the repo** at a hardcoded `~/dev-projects/sdlc-ai/user_projects`
(`project_tool.py:43`). There is no database and **no file locking** anywhere.

**Integrations:** LLM calls default to a local LM Studio server (`localhost:1234`), OpenCode is an
HTTP client to a server on `:4096`, the dashboard is FastAPI + a Vue SPA (`radar.html`) on
`0.0.0.0:8080`, and Telegram uses the raw Bot API for clarification Q&A.

### README promises vs. reality

| README claim | Status |
| :--- | :--- |
| Kanban board (`Backlog → In Progress → Testing → Architect Review → Done`) | ✅ Implemented |
| Contract-first interface specs | ✅ Implemented (generation, injection, a contract gate) |
| Three-key sign-off (Dev → Test → Architect) | ⚠️ Implemented **but unsound** (P0-1) and bypassable (P0-2) |
| Event log / state store | ✅ Implemented |
| Human clarification / notification | ✅ Implemented (Telegram + dashboard) |
| Interface-gap escalation | ⚠️ `report_interface_gap` exists but has **zero callers** (dead) |
| Model routing (manual per-agent) | ⚠️ One global model only; `preferred_model` is a dead field |
| Model routing (auto complexity tiers) | ❌ Not implemented |
| Daily Telegram progress report | ❌ Not implemented (no scheduler anywhere) |
| `get_secret(key)` tool | ❌ Does not exist; secrets read from plaintext env/`config.yaml` |
| Release Manager (build/deploy) | ❌ Role YAML only, no dispatch path |
| Infrastructure Analyst (log monitoring) | ❌ Role YAML only, no dispatch path |
| Proactive error webhooks | ❌ No inbound webhook route |

---

## The scrum gap

The system has specialists and a board; it lacks the cadence and the feedback loops that make a
group of specialists *a team*. This is the heart of "I want it to work like a scrum team."

| Scrum mechanic | Current state | Evidence |
| :--- | :--- | :--- |
| Product Owner | Implied ("the human") but never modeled as a role | — |
| Scrum Master | The "Orchestrator" coordinates but isn't a named role and enforces no ceremony | — |
| Product backlog | Stories persist, but "backlog" is a *derived* state, not a curated queue; Engine A creates and immediately runs every story | `story_tool.py` derives status from child tasks |
| Prioritization | **Dead field.** `priority` is stored and settable but read by nothing; execution order is alphabetical by story ID | `story_tool.py:294,318` |
| Sprints / timeboxes | **Absent** — no sprint entity, dates, capacity, or budget | zero matches repo-wide |
| Sprint planning | **Absent** — decomposition is a one-shot LLM call, no scope selection or sizing | `project_tool.py:308` |
| Daily standup | **Absent** despite README promising a 24h digest; no scheduler exists | — |
| Review / acceptance | **Absent** — no story is ever shown to a human to accept; `acceptance_criteria` is stored but **never read by any gate** | `story_tool.py:99` |
| Retrospective | **Absent** — the single highest-value missing piece for an *autonomous* team | — |
| Estimation / velocity | **Absent** — no points, no throughput metric | — |
| WIP limits | **Absent** for work; `MAX_CONCURRENT_AGENTS=2` is a process cap on a path that is serial anyway | `orchestrator_daemon.py:26` |
| Definition of Done | Partially implemented as the 3-key gate, but unsound, bypassable, and it never checks the story's own acceptance criteria | see P0-1, P0-2 |

The retrospective deserves emphasis: it is the reason to run scrum rather than a flat pipeline. An
agent that mines the event log each sprint for failure patterns (rejection loops, gate failures,
budget overruns) and proposes playbook changes is what turns this from "a pipeline that runs once"
into "a team that gets better." The event log you already have is exactly the raw material for it.

---

## Defects, in priority order

The four reviews found roughly ninety issues between them; the full catalogs live in the review
notes. What follows is the load-bearing subset — the ones that block "works like a scrum team,"
grouped by severity. File:line references are to `7ee5435`.

### P0 — Correctness blockers (the system does not reliably work)

**P0-1 · The stage-gate verdict parser accepts rejections.** Both quality keys decide pass/fail by
substring:
```python
passed   = "PASS" in content.upper() or "APPROVE" in content.upper()   # stage_gate_tool.py:113
approved = "APPROVE" in content.upper() or "PASS" in content.upper()   # stage_gate_tool.py:201
```
An LLM reply of `FAIL — this does not pass the acceptance criteria` contains "PASS"; `NOT APPROVED`
contains "APPROVE". Both keys of the three-key sign-off false-positive on their own rejection text.
This was independently flagged by three of the four reviews. **This is the most important single
fix** — it is the difference between "Done" meaning something and not. Fix: anchor the parse to the
leading verdict token the prompts already mandate (`PASS`/`FAIL`, `APPROVE`/`REJECT`).

**P0-2 · Two of three execution engines bypass the gates, and one can never run.** `core/runtime.py`
(Engine B) sets `done`/`failed` directly with no gates (`:543-544`); `worker.py` (Engine C) does the
same (`:187`). Worse, both `agent_executor.py:84` and `worker.py:137` call `printf(...)` — not a
Python builtin — so Engine C raises `NameError` immediately and has demonstrably never run
successfully. And the daemon (Engine B) can pick up a `pending` task that Engine A is already
executing (`orchestrator_daemon.py:141`), double-spawning a second, gate-less executor. **Decide on
one engine.** Engine A (gated) is the one to keep; Engine B needs to route through the gates or be
retired; Engine C should be deleted.

**P0-3 · The QA gate never runs a test.** `run_qa_gate` sends only the task description and the
developer's *self-reported* completion notes to an LLM for a prose verdict (`stage_gate_tool.py:97-104`);
it never reads code or executes a suite, directly contradicting the README's "executes automated
suites." Combined with P0-1, "Testing Passed" is one LLM rubber-stamping another LLM's claims about
work neither of them ran.

**P0-4 · The retry/escalation ladder is inert.** `handle_failure` reads `retry_count` from the task
and writes it back only to the in-memory copy (`orchestrator_tool.py:544-547`); `add_task` never
creates the field and nothing persists it. So it is always 0, `MAX_RETRIES` has no effect, and the
re-assign and escalate branches (`:570-632`) are unreachable dead code. There is no circuit breaker:
a failing task has no bounded, visible path to a human.

**P0-5 · The dependency-aware execution path is never called.** `run_story_orchestration`
(`orchestrator_tool.py:743`, 125 lines) is the *only* code that honors inter-story dependencies and
blocking — exactly the sequencing a scrum backlog needs — and nothing invokes it. The live path
(`project_tool._continue_to_execution:717`) runs stories in raw LLM-emitted order with no dependency
concept.

**P0-6 · A completed project breaks its own status endpoint.** `get_project_status` reads
`results[-1]["summary"]` (`project_tool.py:792`), but the results dict has no `summary` key →
`KeyError` on every completed project, via `GET /api/projects/{id}` (`pulse_server.py:562`).

**P0-7 · Concurrency corrupts the state store.** Every mutation is a non-atomic read-modify-write of
a shared JSON file with no lock (`bootstrap.py:48,80-100`). Two concurrent `add_task` calls read the
same `next_id` and mint **duplicate task IDs** (`:81-82`), which then collide in every
`id == task_id` lookup. With a daemon, a watchdog thread, and pipeline threads all writing, updates
are silently lost.

**P0-8 · Infrastructure that is broken by default.** Telemetry posts to port **8081** while the
server listens on **8080** (`agent_manager.py:67`, `runtime.py:565` vs `pulse_server.py:665`), so
every heartbeat fails and the dashboard's live view is cosmetic — the warning is even swallowed at
`runtime.py:336`. `opencode_tool.start_server()` is a stub that returns success without starting
anything (`:222`), so developer tasks fail with a misleading "server not running" the moment they
try to execute.

### P1 — Security (blocks any deployment beyond localhost)

**P1-1 · Unauthenticated RCE chain.** The dashboard binds `0.0.0.0` (`pulse_server.py:665`,
`command_center_tool.py:21`) with **no authentication on any endpoint**. `POST /api/projects/submit`
kicks off the LLM pipeline, which can invoke an unsandboxed `shell=True` executor
(`shell_executor.py:10-16`, exposed to agents as the `shell` tool) and unrestricted file
read/write/delete (`file_manager.py`, no path confinement). Anyone on the network has a path to host
command execution.

**P1-2 · Shell injection via LLM-authored commit messages.** `git_tool` interpolates unescaped
strings into `subprocess.run(shell=True)`: `f'git add . && git commit -m "{message}"'`
(`git_tool.py:53`). A commit message (which is LLM-generated) containing `"`, `` ` ``, or `$(...)`
executes arbitrary commands.

**P1-3 · Secrets exposed.** `config.yaml` — the documented home for `llm.api_key` — is **not
gitignored** (contradicting the README's own "secrets never in the repo"). The Telegram bot token is
readable by anyone via `GET /api/state` (`pulse_server.py:384`, which dumps `project_config`). And
`agent_logger` harvests `API_KEY`/`LLM`/`OPENCODE`-prefixed env vars into `.sdlc/*.jsonl` files that
`.gitignore` doesn't cover (`agent_logger.py:19-30,141`).

**P1-4 · Telegram has no user authorization.** `_process_update` never checks `message["from"]["id"]`
(`telegram_bot.py:180-224`); it matches only chat + reply-to. In a group chat, any member can answer
clarifications and steer the autonomous pipeline.

**P1-5 · CORS `*` with credentials.** `allow_origins=["*"]` together with `allow_credentials=True`
(`pulse_server.py:78-84`) — an invalid, unsafe combination that lets any page the operator visits
drive every state-changing endpoint.

### P2 — Trust, hygiene, and correctness-in-the-large

- **`resume_project(auto_answer=True)` fabricates the human-in-the-loop.** It auto-answers *every*
  pending clarification with the canned string `"Proceeding with requirements as stated."`
  (`clarification_tool.py:148`, `project_tool.py:600`). Useful for testing; dangerous as a default,
  because it silently rubber-stamps the LLM's own ambiguity flags.
- **Two `update_task_status` implementations with different side effects** — `registry_tool`'s
  version skips the per-task file, the kanban sync, and the event (`registry_tool.py:65` vs
  `bootstrap.py:109`). The orchestrator uses the lossy one everywhere, so the board goes stale on
  every orchestrator-driven transition.
- **The role system is largely decorative.** All 9 roles declare `preferred_model: local` (a field
  no code reads), `business_analyst.yaml` is never loaded (the BA phase uses an inline prompt), only
  4 of 9 roles are reachable in the main pipeline, and no role's `allowed_tools` includes the
  OpenCode tools its own developers need.
- **Packaging is broken** — `pip install .` omits top-level `bootstrap.py` and all `roles/*.yaml` +
  `radar.html` data files, so an installed copy fails on first import. No `[project.scripts]`, no run
  instructions in the README.
- **Committed junk:** `debug_log.txt` (a captured failing run), `reconstruct.py` (a 1-line stub),
  `test_opencode_success.py` / `test_opencode_retry.py` (1-line `print` files that pytest could
  collect), and machine-specific `/Users/klschaefer/...` paths in seven files.
- **The event stream is disabled server-side** (`_safe_read_events` returns `[]`,
  `pulse_server.py:106`) while the UI still expects it, so the pipeline ticker is dead. The dashboard
  refreshes only via a 5s poll.

### The test suite (blocks verifying any of the above)

The suite cannot currently function as a safety net:

- **It does not run green.** `tests/test_project_tool.py` fails at *collection* with
  `ImportError: PROJECTS_DIR` (renamed to `USER_PROJECTS_ROOT`), and 44 of the collectible tests
  fail — the majority from cross-test state leakage, not real logic errors (many pass in isolation).
- **Test drift:** whole files target APIs that were refactored away — `opencode_tool`'s
  `_server_process`/`_server_port` (11 dead tests), `pulse_server`'s `TASK_REGISTRY_PATH` and the
  `/ws` WebSocket path (now `/radar-ws`), `command_center`'s `URL`.
- **Destructive fixtures:** `test_interface_tool.py:43` overwrites the real
  `state/project_state.json` 25× per run; other tests write into the real registry and event log;
  `test_project_tool.py` leaks directories into `~/dev-projects/...`.
- **Vacuous tests:** several wrap assertions in `except Exception: pass` (the WebSocket tests can
  never fail) or assert only that a dict has the right keys.
- **The actual engine is untested and structurally untestable.** `core/runtime.py` (580 lines),
  `core/orchestrator_daemon.py`, and `worker.py` have zero tests, and because they
  `sys.path.insert` and `import llm_tool` as a *top-level* module, the `patch("tools.llm_tool...")`
  convention every other test uses has no effect on them (dual module identity).
- **No CI**, no `.github/`, no test instructions in the README.

The good news: `test_story_tool.py` and `test_stage_gate_tool.py` are genuinely good behavioral
tests. The pattern exists; it needs to be made hermetic (a shared `tmp_path` fixture, no global
mutation) and extended to the engine.

---

## Where to start

The full sequence is in [ROADMAP.md](ROADMAP.md); the short version:

1. **Make the suite green and add CI** (P0 test items). Without this, nothing below is verifiable.
   Fix the `PROJECTS_DIR` import, delete the drifted/vacuous tests, add a hermetic `tmp_path`
   fixture in `conftest.py`, and add a GitHub Actions workflow. This is also the natural place to
   set up a SessionStart hook so future Claude-on-web sessions can run the tests.
2. **Fix the Definition of Done** (P0-1, P0-3) and **collapse to one execution engine** (P0-2).
   This is what makes "Done" trustworthy — the precondition for a scrum team.
3. **Close the security holes** before anything leaves localhost (P1-1…P1-5), starting with
   gitignoring `config.yaml` and binding to `127.0.0.1`.
4. **Add the scrum layer** on top of the now-trustworthy pipeline — sprints, planning, review/
   acceptance, and the retrospective — per [SCRUM_PROCESS.md](SCRUM_PROCESS.md).

I have not opened a PR or filed issues, since you didn't ask for either — say the word and I can turn
the P0/P1 items into tracked issues, or start on step 1.
