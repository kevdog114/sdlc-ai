# Work Intake & Story Tracking

> **Implementation status (2026-08-18):** the gaps below are now closed —
> define is separated from execute (submitting ends at a persisted,
> project-scoped backlog + proposed sprint; sprints execute), stories carry
> `project_id`/points/planned_tasks, and modifications flow through
> `submit_change_request` as backlog deltas with the story ledger as BA
> context. This document is kept as the analysis of the pre-change design
> (`7ee5435`); §4 describes what was built.

How you give the team work (new projects and modifications), whether that intake is sufficient, and
how well the team tracks stories — both the already-implemented ones and the defined-but-not-yet-
worked backlog. Companion to [REVIEW.md](REVIEW.md); references are to `7ee5435`.

**Bottom line:** intake is adequate for firing off a *new* project once, but there is no real model
for *iterative modifications*, and there is no durable **backlog**. Work is generated and executed in
a single synchronous pass, so nothing is ever "defined but not yet worked on," and the team keeps no
project-scoped memory of what it already built.

---

## 1. How work gets in today

| Path | Entry | Purpose |
| :--- | :--- | :--- |
| New project (UI) | `POST /api/projects/submit` (`pulse_server.py:484`) → `submit_project` | Greenfield: name + description |
| New project (CLI) | `pipeline_test.py "desc" "name"` | Same, plus auto-answers clarifications |
| "Modification" | `POST /api/projects/{id}/command` (`pulse_server.py:579`) | The only path to an *existing* project |
| Raw task (daemon) | task rows in `task_registry.json`, polled by `orchestrator_daemon` | Gate-less, separate engine |

**The greenfield flow is reasonable and has a genuine strength.** `submit_project`
(`project_tool.py:490`) runs BA analysis → if the BA finds ambiguities it creates clarification
requests and **pauses** (`status = awaiting_clarification`), pushing questions to the dashboard and
Telegram → on answers it continues to Architect → stories → execution. That clarification loop —
stopping to ask the human before building — is the best part of the intake design and worth keeping.

---

## 2. Why "additional modifications" is not sufficient

The `/command` endpoint is the whole story for changing an existing project, and it does this
(`pulse_server.py:596-604`):

```python
result = project_tool.submit_project(command, project_id=project_id)
```

That re-enters the **full pipeline** with the modification text as the sole input. Concretely
(`project_tool.py:496-571`, `680-734`):

1. `_ba_analyze(command)` analyzes the modification text **in isolation** — it is never combined with
   the project's stored `refined_requirements`, `architecture`, or existing stories.
2. If there are no ambiguities, it runs `_architect_design` **from scratch** (re-architecting the
   whole system around just the change) and then `_create_stories_and_tasks`, which generates a
   **brand-new story set** with no awareness of what already exists.
3. It then **overwrites** the project's record: `project["stories"] = [titles of the new run]` and
   `project["results"] = [new results]` (`:729-730`). The prior run's stories and results are
   dropped from the project.

So a "modification" is really *"throw away the context and re-run the pipeline on the change text
alone."* The consequences:

- **No delta/increment model.** "Add feature X to what you built" is impossible — the team sees only
  "add feature X," designs a system for just that, and can neither build on the existing code nor
  detect conflicts with it.
- **The architecture churns** every time, because `_architect_design` re-runs fresh.
- **Prior work is orphaned.** Old stories linger in the global registry (§3) but the project forgets
  them, and the new planning never sees them.
- **Not idempotent.** Re-submitting the same thing regenerates a different, non-deterministic story
  set (LLM at `temperature=0.3`), duplicating work.
- Related bug (REVIEW P0 / D20): even a plain resume re-analyzes the *passed* description rather than
  the *stored* requirements, so the input to a resumed run is easy to get wrong.

Also missing from intake entirely:
- **No bug/error intake.** The README's "production errors → bug stories via webhook" is
  unimplemented, so real-world feedback can't become work.
- **No way to add a single story or task** to a project. The dashboard's "prompt" tab only triggers a
  full BA re-run; there is no "append this one story to the backlog."
- **Everything is synchronous and blocking.** `submit_project` runs BA → architect → *every story
  executed* before it returns. Giving the team a project means blocking until the entire thing is
  built or fails; you cannot hand it work incrementally.

---

## 3. How well it tracks stories (implemented vs. backlog)

This is the weaker half, and it stems from one root cause: **stories are created and executed in the
same synchronous loop, so a persistent backlog never exists.**

**3.1 · There is no waiting backlog.** `_continue_to_execution` generates the stories and immediately
runs all of them in order (`project_tool.py:717-727`):

```python
for idx, story in enumerate(stories):
    story_result = _execute_story_tasks(story=story, ...)
```

Every story is worked the instant it is defined. Nothing is ever "defined but not yet worked on" —
the state the question asks about is, at runtime, only a fraction of a second wide.

**3.2 · Story status is derived, never stored.** `derive_story_status` (`story_tool.py`) computes a
story's column from its child task states on every read. A story appears in "backlog" only
transiently, before its tasks start; there is no durable "planned for later" state a human can put a
story into and trust.

**3.3 · Stories are not linked to projects.** `create_story` takes only
`title, description, acceptance_criteria, priority, dependencies` — **there is no `project_id`**
(`story_tool.py:71-106`), and the story record has no project field. The global
`story_registry.json` is a flat, append-only pile. You **cannot ask "which stories belong to project
X."** The project record itself keeps only the *titles* (plain strings) of the most recent run
(`project_tool.py:729`) — losing the IDs, statuses, acceptance criteria, and everything from any
earlier run.

**3.4 · No "already implemented" ledger informs planning.** When a change comes in, story generation
gets the architecture and requirements but **no list of what is already built**. It cannot avoid
re-implementing, cannot build on top, and cannot flag conflicts. The event log and knowledge base
*do* record history, but the planning prompts never read them back (this is the same unclosed loop
described in [AGENTS_AND_PROMPTS.md](AGENTS_AND_PROMPTS.md) §4).

**3.5 · Accumulation without dedup.** Each modification appends a fresh set of `STORY-N` records with
no dedup or diff against prior ones, so repeated runs silently fill the registry with overlapping
stories and no marker of which are current.

**3.6 · What *does* work.** At a point in time, the dashboard's task-kanban and story-kanban reflect
current statuses derived from the registries, so you can **see what is done / in progress right
now**, and the append-only event log is a complete history of what happened. "What happened" is
well-recorded; "what is the durable, project-scoped backlog" is not.

---

## 4. What it would take

The intake and tracking gaps are the same gap the scrum layer needs to close
([SCRUM_PROCESS.md](SCRUM_PROCESS.md)), because a backlog you keep adding to *is* the thing a scrum
team operates on. The minimum changes:

1. **Separate "define" from "execute."** Story generation should persist stories to a backlog and
   return; execution should be a separate step that *pulls* from the backlog. This alone creates the
   "defined but not yet worked" state that doesn't exist today, and it turns the synchronous
   fire-once call into something you can steer.
2. **Scope stories to a project.** Add `project_id` to `create_story` and the story record so the
   backlog is queryable per project, and stop overwriting the project's story list on each run —
   append and track status instead.
3. **Make modification a delta, not a re-run.** A change request should load the existing
   requirements + architecture + the list of done/pending stories, give all of that to the BA/
   Architect as context, and produce *new or amended* stories that reference what exists — not a
   from-scratch regeneration.
4. **Add granular intake.** Let a human add or edit a single story/task directly (a change request,
   a bug), and implement the error-webhook path so production issues enter as backlog bug stories.
5. **Feed planning the "already built" ledger** (ties to the RAG-planning item in
   [AGENTS_AND_PROMPTS.md](AGENTS_AND_PROMPTS.md) §4.1) so the team stops forgetting its own work.

Items 1–2 are the foundation; without a persistent, project-scoped backlog, none of the scrum
ceremonies (planning, review, retrospective) have anything to operate on.
