# Roadmap

A phased plan from the current state (a partially-working pipeline with an unsound quality gate and
no test safety net) to a trustworthy AI scrum team. The ordering is deliberate: **you cannot safely
fix the pipeline until the tests run, and you cannot add the scrum layer until the pipeline is
trustworthy.** Each phase has an exit criterion — it isn't done until that is demonstrable.

Severity tags (P0/P1/P2) reference [REVIEW.md](REVIEW.md).

## Phase 0 — Make it verifiable *(do this first)*

Nothing below can be done safely without a working test suite, because every fix risks a regression
you currently cannot detect.

- Fix the collection error: `tests/test_project_tool.py` imports `PROJECTS_DIR` (now
  `USER_PROJECTS_ROOT`).
- Add a hermetic `tmp_path`-based fixture in `conftest.py` that redirects all `bootstrap` state
  paths and restores them on teardown; remove the per-test global mutation that leaks between files.
- Delete or rewrite the drifted tests (the `opencode_tool` stubs, the `pulse_server` `/ws` and
  `TASK_REGISTRY_PATH` tests, the `command_center` `URL` tests) and the vacuous ones
  (`except Exception: pass` WebSocket tests).
- Stop the destructive fixture in `test_interface_tool.py:43` from overwriting real state.
- Add a **GitHub Actions CI workflow** (lint + `pytest`) and a **SessionStart hook** so
  Claude-on-web sessions can run tests and linters. Remove the committed junk (`debug_log.txt`,
  `reconstruct.py`, the two 1-line root `test_*.py` files) and gitignore `config.yaml`, `.sdlc/`,
  `sdlc-logs/`, `.pytest_cache/`.

**Exit:** `pytest` is green in CI, runs are hermetic (no writes outside a tmpdir), and a red test
blocks merges.

## Phase 1 — Make "Done" mean something

The Definition of Done is the foundation of a scrum team. Fix it before building on it.

- **P0-1:** replace the substring verdict parser in `stage_gate_tool.py:113,201` with a parse
  anchored to the leading verdict token (`PASS`/`FAIL`, `APPROVE`/`REJECT`). Add tests for the
  false-positive cases ("does not pass", "not approved").
- **P0-3:** make the QA gate actually run the project's tests (execute the suite in the sandbox and
  read the result) instead of grading self-reported notes.
- **P0-2:** collapse to **one execution engine**. Keep the gated pipeline (Engine A); route the
  daemon's `core/runtime.py` through the same gates or retire it; delete the orphaned, crash-broken
  `agent_executor.py` + `worker.py` (Engine C).
- **P0-4:** persist `retry_count` and implement the rejection circuit breaker (2× → `Blocked: Needs
  Human` with context). This makes the existing `handle_failure` re-assign/escalate code reachable.
- Consolidate the two `update_task_status` implementations so the board never goes stale.

**Exit:** a deliberately-broken task is caught by the gate and lands in `Blocked: Needs Human` after
two rejections; a passing task merges only when tests actually ran and passed; there is exactly one
code path that writes `done`.

## Phase 2 — Make it safe to run off-localhost

Required before the dashboard or Telegram bot is exposed to anyone but you.

- **P1-1:** bind to `127.0.0.1` by default; add authentication (a token/key) to every state-changing
  endpoint; sandbox the shell and file tools (container + path confinement + allowlist).
- **P1-2:** stop interpolating into `shell=True` — pass argv lists to `subprocess.run`, especially
  for LLM-authored commit messages.
- **P1-3:** gitignore `config.yaml`; stop dumping `project_config` (with the bot token) from
  `GET /api/state`; stop harvesting secret-prefixed env vars into agent logs. Implement the README's
  `get_secret(key)` tool as the single path to credentials.
- **P1-4/P1-5:** add a Telegram user-id allowlist; fix the `CORS *` + credentials combination.

**Exit:** a security pass confirms no unauthenticated path reaches code execution, file writes, or
secrets, and no secret is committable or network-readable.

## Phase 3 — Repair the observability and dev loop

The system's live-view and developer experience promises are currently cosmetic.

- **P0-8:** fix the 8080/8081 telemetry port mismatch and un-swallow the failure warning; make
  `opencode_tool.start_server()` actually manage the server (or fail loudly), and honor the port it
  found rather than hardcoding 4096.
- Re-enable the event stream the UI expects (`_safe_read_events` returns `[]` today) and broadcast
  task *updates*, not just appends.
- **P0-6:** fix the `KeyError: 'summary'` that breaks `get_project_status` for completed projects.
- Fix packaging (`py-modules = ["bootstrap"]`, `package-data` for roles + `radar.html`, move `httpx`
  to dev, drop the unused telegram extra), add `[project.scripts]`, de-hardcode the
  `/Users/klschaefer/...` paths, and write real run instructions in the README.

**Exit:** a fresh `pip install .` runs; the dashboard shows live agent state and pipeline events;
one documented command starts the system end-to-end.

## Phase 4 — The scrum layer

Now that "Done" is trustworthy and the system is safe and observable, add the cadence from
[SCRUM_PROCESS.md](SCRUM_PROCESS.md).

- Add the `Sprint` entity (goal, timebox, token budget, selected stories) to the state store.
- Make `priority` load-bearing and call the currently-dead `run_story_orchestration` /
  `get_ready_stories` for dependency-aware, priority-ordered selection.
- Implement **Sprint Planning** (capacity from velocity + budget, PO approval via the autonomy dial),
  the **Daily Standup** report (a scheduled trigger), **Sprint Review** with per-story PO acceptance
  (finally reading `acceptance_criteria`), and — the highest-value piece — the **Retrospective**
  that mines the event log and proposes versioned playbook changes.
- Add estimation (story points) and velocity/cost-per-point tracking; wire points to model-routing
  tiers so the README's routing finally has an input.
- Replace `auto_answer=True` with the explicit per-ceremony autonomy dial.

**Exit:** one full sprint runs end-to-end — plan → build → gates → review → retro — on a demo
project, within budget, with a retrospective proposal applied to the next sprint.

## Phase 5 — Release & production feedback

Turn the two role YAMLs that have no code today into working agents.

- **Release Manager:** build + deploy via Docker Compose to a configured target; versioning.
- **Infrastructure Analyst:** log scans + the inbound error-webhook route; production errors become
  backlog bug stories via the BA (the README's feedback loop).

**Exit:** a sprint increment auto-deploys to a preview; a thrown exception in the deployed app
arrives in the backlog as a bug story.

## Ongoing from Phase 1: dogfood

Move this repository's own backlog into the system as soon as the pipeline is trustworthy (Phase 1+).
Every phase exit doubles as a sprint review of the system, run by the system — the fastest way to
find the next round of defects is to make it build itself under its own (now sound) gates.
