# Scrum Process Specification (target operating model)

> **Implementation status (2026-08-18):** the core loop is now implemented —
> project-scoped backlog with story points, sprint plan/start/review/complete
> (`tools/sprint_tool.py`), PO acceptance with rejection-to-backlog, velocity
> from accepted points, and the event-log retrospective (proposals recorded,
> not self-applied). Still open: timebox/budget enforcement (§3), the
> scheduled standup report (§4.2), the autonomy-dial config surface (§8), and
> continuous refinement (§4.5).

How SDLC-AI should operate as a scrum *team* rather than a one-shot pipeline. This layers on top of
the existing workflow — it reuses the stories, the kanban board, the stage gates, the clarification
loop, and the event log that are already implemented — and adds the cadence and feedback loops that
are missing (see the scrum-gap table in [REVIEW.md](REVIEW.md)).

Each section notes **what exists today** and **what to add**, so this doubles as a build spec.

## 1. Roles

| Scrum role | Played by | Exists today? | Responsibilities |
| :--- | :--- | :--- | :--- |
| **Product Owner** | The human user | Implicit only | Vision, backlog priority, sprint-goal approval, story acceptance, the autonomy dial |
| **Scrum Master** | The **Orchestrator** | Coordinates but enforces no ceremony | Run the sprint loop and ceremonies, enforce gates + WIP, detect impediments, escalate |
| **Development Team** | The specialist agents | 4 of 9 reachable | BA, Architect, Developer, QA/Test, (later) Infra + Release |

The Orchestrator is a coordinator, never a super-agent: it schedules, moves state, enforces rules,
and escalates — it does not write requirements or code itself. Making it an explicit role (rather
than the loose set of functions in `orchestrator_tool.py` / `orchestrator_daemon.py` today) is the
first structural change.

## 2. Artifacts

| Artifact | Definition | Exists today? |
| :--- | :--- | :--- |
| **Product Backlog** | All stories, ordered by PO priority, continuously refined | Stories persist; ordering is alphabetical, `priority` is unread — **make priority load-bearing** |
| **Sprint Backlog** | Stories selected for the current sprint + the sprint goal | **New** — no sprint entity exists |
| **Increment** | The integrated, gate-passing state at sprint end | Implicit in task `done` states |
| **Definition of Done** | §5 | Partially (the 3-key gate) — **must be fixed first, see REVIEW P0-1** |
| **Definition of Ready** | Acceptance criteria + estimate + resolved deps + interface-spec merged | `acceptance_criteria` is stored but unread — **wire it in** |

## 3. The sprint (new)

- **Timebox** (`sprint.length`, default 1 day). AI agents deliver in hours; a short sprint gives the
  human a natural daily supervision point.
- **Budget** (`sprint.budget`, tokens or $). Capacity is whichever binds first — budget or velocity.
  A sprint that exhausts its budget stops starting tasks and goes to review early. This is also the
  hard cap on autonomous spend that the system currently lacks entirely.
- **Sprint goal**: one sentence. Every selected story must serve it.

A `Sprint` object (id, goal, timebox, budget, selected story IDs, state) belongs in the state store
next to the story and task registries.

## 4. Ceremonies

All ceremonies are automated events in the sprint loop; the PO participates asynchronously. Every
ceremony writes to the existing `logs/event_log.jsonl`.

### 4.1 Sprint Planning — *new*
1. Orchestrator computes capacity (min of budget and rolling velocity).
2. BA proposes the top-of-backlog **Ready** stories that fit; Architect confirms dependency order.
3. Orchestrator drafts the sprint goal and requests PO approval via the existing clarification
   channel. Autonomy dial: `plan.approval = required | auto-after-timeout | full-auto`.

Reuse: this is where the currently-dead `run_story_orchestration` (the only dependency-aware path,
`orchestrator_tool.py:743`) and `get_ready_stories` should finally be called.

### 4.2 Daily Standup — *partially exists*
The README already promises a 24h digest; no scheduler exists to produce it. Add one (a cron/
scheduled trigger) that emits, per agent: **done / next / blockers**, plus budget consumed vs.
remaining. Blockers needing a human carry an explicit question the PO can answer by replying —
reusing the Telegram reply plumbing that today only handles clarifications.

### 4.3 Sprint Review — *new*
1. Orchestrator assembles the increment: stories completed, demo artifacts (deployed preview,
   test reports, diffs), completion notes.
2. PO accepts or rejects **each story**. This is the acceptance ceremony the system has nowhere
   today — and the point at which the stored `acceptance_criteria` finally gets checked.
3. Rejected stories return to the backlog with PO notes; the BA re-refines before they are Ready
   again. Only accepted stories count toward velocity.

### 4.4 Retrospective — *new; highest-value addition*
The reason to run scrum rather than a flat pipeline, and the compounding advantage of an autonomous
team:
1. An analysis agent (high-reasoning model) mines the sprint's slice of the event log for patterns:
   rejection loops, gate failures by type, interface gaps, budget hot spots, estimate-vs-actual
   variance.
2. It produces up to 3 concrete **process-change proposals**, each targeting one lever — an agent
   playbook/prompt, a model-routing rule, a gate definition, or a Ready/Done criterion.
3. PO approves (autonomy dial). Approved changes are versioned in the state store and take effect
   next sprint.
4. Each proposal records the metric it expects to move, so the next retrospective can judge it and
   revert failed experiments.

The event log you already have is exactly the substrate this needs; nothing new has to be captured
to start.

### 4.5 Backlog Refinement — *partially exists*
The BA keeps ~2 sprints of backlog Ready: decomposing upcoming stories, attaching acceptance
criteria, flagging interface-spec work, and estimating with the Architect. Today decomposition is a
single one-shot call at execution time; make refinement continuous and separate from execution.

## 5. Definition of Done

**Prerequisite: fix the gate (REVIEW P0-1, P0-2, P0-3) before relying on any of this.** A story is
Done only when every task passes the three-key sign-off *and*:

1. Code merged via the task's PR(s) — no direct pushes.
2. QA gate green: tests actually **created/updated and executed** (not an LLM grading self-reported
   notes, as today).
3. Architect approval: consistent with architecture and interface specs.
4. Interface spec updated if the contract changed.
5. Completion notes written.
6. The increment builds (and, where configured, deploys to a preview).

PO acceptance happens at Sprint Review and is deliberately *outside* the DoD: the team finishes work
autonomously, but only the human decides it delivered the intent.

## 6. Estimation and velocity (new)

- **Story points** (1/2/3/5/8), assigned at refinement by BA + Architect. An 8 must be split before
  it is Ready.
- Points map to the model-routing tiers (1-2 → light models, 3-5 → coding models, 8/decomposition →
  high-reasoning), so estimation drives routing — the mechanism the README describes but doesn't
  implement.
- **Velocity** = PO-accepted points per sprint (rolling window of 3).
- **Cost per point**, tracked per agent and model, feeds planning capacity and the retrospective.

## 7. Flow rules and circuit breakers

Story state machine (reusing existing statuses where they exist):

```
Backlog → Ready → Sprint Backlog → In Progress → Testing → Architect Review → Done → Accepted
                                       ▲             │              │
                                       └── Rejected ─┴──────────────┘
```

- **Rejection loop limit** (fixes REVIEW P0-4): a task rejected 2× stops retrying and becomes
  `Blocked: Needs Human` with full failure context. Never silently loop; never silently abandon.
  This requires persisting `retry_count`, which today is never written.
- **WIP limit**: at most N stories In Progress (default = number of developer agents), enforced on
  the board — distinct from the process-level `MAX_CONCURRENT_AGENTS`.
- **Interface gaps**: wire up the existing-but-uncalled `report_interface_gap` so a developer that
  hits a contract gap blocks and escalates instead of patching around it.
- **Mid-sprint bugs**: production errors (once the feedback loop exists) become backlog bug stories;
  only the PO — or a defined `auto-hotfix` policy — pulls work into a running sprint.

## 8. Configuration surface

All policy lives per-project in the state store:

```yaml
sprint:
  length: 1d
  budget: 5_000_000      # tokens (or $) per sprint — the spend ceiling
autonomy:
  plan.approval: required        # required | auto-after-timeout | full-auto
  retro.approval: required
  hotfix: off                    # off | sev1-only | auto
flow:
  wip_limit: 2
  rejection_limit: 2
reporting:
  standup: telegram
```

The autonomy dial is the trust throttle: start with approvals required everywhere and loosen
per-ceremony as the retrospective history shows the team earning it. Note that today's
`auto_answer=True` shortcut (which rubber-stamps every human question) is the *opposite* of this —
it should be replaced by the explicit dial.
